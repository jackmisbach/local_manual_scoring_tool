"""Shared helpers for the dev scripts: Hawk API access + sample resolution.

Both ``download_starting_image.py`` and ``download_sample.py`` import this. It
mirrors the resolution flow in METR/manual-scoring-tool's ``scoring/infra.py``
but is self-contained (stdlib + the ``hawk`` CLI; ``aws``/``docker`` are driven
by the individual scripts).
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from functools import cache
from typing import Any

# ECR registry holding the inspect task images. The starting image for a sample
# is "{repo}:{task_name}-{task_version}".
ECR_REGISTRY = "328726945407.dkr.ecr.us-west-2.amazonaws.com"
ECR_TASKS_REPO = f"{ECR_REGISTRY}/prd/inspect-tasks"

# The seed row Hawk creates per sample; its scorer name is this literal string,
# and its metadata holds the canonical artifact_url (s3_url for older samples).
INFO_ROW = "manual_scorer"

# Score metadata records the artifact location as an S3 URL. Artifacts in Hawk's
# bucket are read through the Hawk API with ``hawk download``.
_ARTIFACT_BUCKET = "prd-metr-inspect"

_access_token: str | None = None


# ---------------------------------------------------------------------------
# Hawk API
# ---------------------------------------------------------------------------


def get_access_token(stale: str | None = None) -> str:
    """Return the Hawk access token, fetching it lazily and caching it.

    Pass the expired JWT as ``stale`` after a 401 to force a refresh.
    """
    global _access_token
    if _access_token is not None and _access_token != stale:
        return _access_token
    result = subprocess.run(["hawk", "auth", "access-token"], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("hawk auth failed; run: hawk login")
    _access_token = result.stdout.strip()
    return _access_token


@cache
def _get_api_url() -> str:
    result = subprocess.run(["hawk", "config"], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if line.startswith("HAWK_API_URL="):
            return line.split("=", 1)[1]
    return "https://api.hawk.prd.metr.org"


def hawk_api_get(path: str, params: dict[str, str] | None = None) -> Any:
    """GET ``path`` from the Hawk API and parse the JSON response.

    Refreshes the token once and retries on HTTP 401.
    """
    api_url = _get_api_url()
    url = f"{api_url}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    def send(token: str) -> Any:
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    token = get_access_token()
    try:
        return send(token)
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
        return send(get_access_token(stale=token))
    except urllib.error.URLError as e:
        if isinstance(e.reason, socket.gaierror) and ".hawk." in api_url:
            print(
                f"Error: could not resolve {urllib.parse.urlparse(api_url).netloc}."
                + " Connect to Tailscale (and disable any VPN that hijacks DNS).",
                file=sys.stderr,
            )
            sys.exit(1)
        raise


# ---------------------------------------------------------------------------
# S3 artifact URLs
# ---------------------------------------------------------------------------


def hawk_artifact_path(artifact_url: str) -> tuple[str, str] | None:
    """Split an artifact URL in Hawk's bucket into its eval set ID and folder path.

    e.g. ``s3://<bucket>/evals/<eval_set_id>/artifacts/<uuid>`` ->
    ``("<eval_set_id>", "artifacts/<uuid>")``, as ``hawk download`` takes them.
    Returns None for a URL outside Hawk's bucket.
    """
    prefix = f"s3://{_ARTIFACT_BUCKET}/evals/"
    if not artifact_url.startswith(prefix):
        return None
    eval_set_id, _, folder_path = artifact_url[len(prefix) :].rstrip("/").partition("/")
    return eval_set_id, folder_path


def info_row_artifact_url(info_metadata: dict[str, Any] | None) -> str:
    """Return the manual-scoring artifact URL from an INFO-row metadata dict.

    Reads ``artifact_url`` first, falls back to ``s3_url`` for older samples.
    """
    metadata = info_metadata or {}
    return metadata.get("artifact_url") or metadata.get("s3_url") or ""


# ---------------------------------------------------------------------------
# UUID -> task / image / artifact resolution
# ---------------------------------------------------------------------------


def _fetch_task_version(eval_set_id: str, filename: str) -> str:
    """Read the task version from the eval log headers."""
    header_path = f"{eval_set_id}/{filename}"
    quoted = urllib.parse.quote(header_path, safe="/")
    headers = hawk_api_get("/view/logs/log-headers", {"file": quoted})
    if isinstance(headers, list) and headers:
        eval_info = headers[0].get("eval", {})
    elif isinstance(headers, dict):
        eval_info = headers.get("eval", {})
    else:
        eval_info = {}
    task_version = eval_info.get("task_version")
    if not task_version:
        raise RuntimeError(f"could not determine task version from log headers for {filename}")
    return str(task_version)


def resolve_sample(uuid: str) -> dict[str, str]:
    """Resolve a sample UUID to its task name, version, and starting image.

    Returns a dict with ``uuid``, ``eval_set_id``, ``filename``,
    ``task_name`` (short), ``full_task_name``, ``task_version``, and ``image``.
    """
    meta = hawk_api_get(f"/meta/samples/{uuid}")
    eval_set_id = meta.get("eval_set_id", "")
    filename = meta.get("filename", "")
    if not eval_set_id or not filename:
        raise RuntimeError(f"sample {uuid} is missing eval_set_id/filename in Hawk metadata")

    samples = hawk_api_get(
        "/meta/samples",
        {"eval_set_id": eval_set_id, "search": uuid, "limit": "10"},
    ).get("items", [])
    sample_item = next((s for s in samples if s.get("uuid") == uuid), None)
    if not sample_item:
        raise RuntimeError(f"could not find sample {uuid} in API")

    full_task_name = sample_item.get("task_name", "")
    task_name = full_task_name.split("/")[-1]
    if not task_name:
        raise RuntimeError(f"sample {uuid} has no task_name in Hawk metadata")

    task_version = _fetch_task_version(eval_set_id, filename)
    return {
        "uuid": uuid,
        "eval_set_id": eval_set_id,
        "filename": filename,
        "task_name": task_name,
        "full_task_name": full_task_name,
        "task_version": task_version,
        "image": f"{ECR_TASKS_REPO}:{task_name}-{task_version}",
    }


def image_tag(image: str) -> str:
    """The tag portion of an image ref (everything after the final ':')."""
    return image.rsplit(":", 1)[-1]


def tar_name(image: str) -> str:
    """Output filename for an image: its task tag with '.' -> '_', plus '.tar'.

    e.g. "...:sunlight-0.1.0" -> "sunlight-0_1_0.tar". Used both as the
    ``docker save`` filename and as ``sample.json``'s ``image`` value.
    """
    return image_tag(image).replace(".", "_") + ".tar"


def fetch_artifact_url(uuid: str) -> str:
    """Return the S3 artifact URL for a sample from its Hawk INFO row."""
    scores = hawk_api_get(f"/meta/samples/{uuid}/scores").get("scores", [])
    info_row = next((s for s in scores if s.get("scorer") == INFO_ROW), None)
    artifact_url = info_row_artifact_url((info_row or {}).get("metadata"))
    if not artifact_url:
        raise RuntimeError(
            f"sample {uuid} is not set up for manual scoring"
            + f" (no '{INFO_ROW}' row with metadata.artifact_url or metadata.s3_url)"
        )
    return artifact_url
