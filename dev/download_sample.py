#!/usr/bin/env python3
"""Stage one or more samples on disk for the local manual scoring tool.

For each run (sample) UUID this populates ``data/samples/<uuid>/`` with:

  - ``project/``    — the agent's project directory, pulled from the sample's
                      S3 artifacts (falls back to ``artifacts/`` for older
                      samples).
  - ``sample.json`` — ``{"task": ..., "image": ...}`` where ``image`` is the
                      task image's tarball name, e.g. ``sunlight-0_1_0.tar``
                      (the file ``download_starting_image.py`` produces). The
                      sample's run UUID is the ``<uuid>/`` dir name itself.

It mirrors the resolution / S3 flow in METR/manual-scoring-tool's
``scoring/infra.py`` but is self-contained (stdlib + the ``hawk`` and ``aws``
CLIs; shared Hawk helpers live in ``_common.py``).

Prerequisites:
  - ``hawk`` CLI and server 4.0.0 or later (for ``hawk download --prefix``),
    logged in (``hawk login``)
  - ``aws`` CLI with the TaskDeveloper SSO role (``aws sso login``), only for
    older samples whose artifacts are outside Hawk

Per-sample folders are written under ``data/samples/`` by default (override with
--output-dir).

Usage:
  ./download_sample.py <uuid> [<uuid> ...]
  ./download_sample.py --from-file uuids.txt
  ./download_sample.py <uuid> --output-dir data/samples --force
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from _common import fetch_artifact_url, hawk_artifact_path, resolve_sample, tar_name

# hawk download saves the eval set ID it's given in these files, making it the
# default for the user's later bare hawk commands, such as `hawk stop`.
_HAWK_LAST_ID_FILES = (
    Path.home() / ".config" / "hawk-cli" / "last-eval-set-id",
    Path.home() / ".config" / "hawk-cli" / "last-job-id",
)


def _hawk_download(eval_set_id: str, prefix: str, target: Path) -> None:
    """Download the files under ``prefix`` in an eval set's folder into ``target``.

    Leaves ``target`` absent if there are none.
    """
    saved_ids = {
        id_file: id_file.read_bytes() if id_file.exists() else None
        for id_file in _HAWK_LAST_ID_FILES
    }
    # hawk writes <dir>/<prefix>..., so download beside target and move that in.
    with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
        try:
            result = subprocess.run(
                # --include-git keeps the project's .git, as `aws s3 cp` did, since
                # scorers review the agent's commits and tags. hawk skips it by
                # default because its config is agent-written and git runs
                # commands that config names.
                ["hawk", "download", eval_set_id, "--prefix", prefix, "--include-git", "-o", tmp],
                capture_output=True,
                text=True,
                check=False,
                # hawk asks "Continue anyway?" on a CLI/server version mismatch when
                # stdin is a terminal, which would hang with its output captured.
                stdin=subprocess.DEVNULL,
            )
        finally:
            # Put the user's defaults back, so their next bare hawk command still
            # targets their own run rather than this eval set.
            for id_file, saved in saved_ids.items():
                if saved is None:
                    id_file.unlink(missing_ok=True)
                else:
                    id_file.write_bytes(saved)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "No files found" in stderr:
                return
            raise RuntimeError(f"hawk download failed: {stderr}")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)  # e.g. files hawk skipped
        Path(tmp, prefix).rename(target)


def _aws_download(s3_prefix: str, target: Path) -> None:
    """Download ``s3_prefix`` recursively into ``target`` with the aws CLI."""
    result = subprocess.run(
        ["aws", "s3", "cp", s3_prefix, str(target), "--recursive"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "Token has expired" in stderr or "refresh failed" in stderr:
            raise RuntimeError("AWS SSO session has expired. Run: aws sso login")
        raise RuntimeError(f"S3 download failed: {stderr}")


def download_project(artifact_url: str, dest_dir: Path, force: bool = False) -> Path:
    """Download the sample's ``project/`` tree into ``dest_dir/project``.

    Artifacts in Hawk's bucket come through ``hawk download``, others (older
    samples) straight from S3. Older samples store the tree under ``artifacts/``
    instead of ``project/``; both are tried and the result is always written to
    ``dest_dir/project``.
    """
    target = dest_dir / "project"
    if target.is_dir() and any(target.iterdir()) and not force:
        print(f"  project/ already present at {target}, skipping (use --force to re-download)")
        return target
    hawk_path = hawk_artifact_path(artifact_url)
    if target.exists():
        shutil.rmtree(target)

    base = artifact_url.rstrip("/")
    for sub in ("project", "artifacts"):
        print(f"  downloading {base}/{sub}/ -> {target} ...")
        if hawk_path is None:
            _aws_download(f"{base}/{sub}", target)
        else:
            eval_set_id, folder_path = hawk_path
            _hawk_download(eval_set_id, f"{folder_path}/{sub}/", target)
        if target.is_dir() and any(target.rglob("*")):
            return target
        # Empty: this prefix didn't exist; try the next candidate.
        if target.exists():
            shutil.rmtree(target)

    raise RuntimeError(f"no project/ or artifacts/ found under {artifact_url}")


def download_one(uuid: str, output_dir: Path, force: bool) -> Path:
    """Populate ``output_dir/<uuid>/`` with ``project/`` and ``sample.json``."""
    print(f"{uuid}:")
    info = resolve_sample(uuid)
    artifact_url = fetch_artifact_url(uuid)
    image_tar = tar_name(info["image"])
    print(f"  task={info['task_name']}  image={image_tar}")

    dest_dir = output_dir / uuid
    dest_dir.mkdir(parents=True, exist_ok=True)

    download_project(artifact_url, dest_dir, force=force)

    sample = {
        "task": info["task_name"],
        "image": image_tar,
    }
    (dest_dir / "sample.json").write_text(json.dumps(sample, indent=2) + "\n")
    print(f"  wrote {dest_dir / 'sample.json'}")
    print(f"  done: {dest_dir}/")
    return dest_dir


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _collect_uuids(args: argparse.Namespace) -> list[str]:
    uuids = list(args.uuids)
    if args.from_file:
        for line in Path(args.from_file).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                uuids.append(line)
    # De-dupe while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for u in uuids:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    return ordered


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stage samples/<uuid>/ (project/ + sample.json) for one or more UUIDs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("uuids", nargs="*", help="One or more run (sample) UUIDs.")
    parser.add_argument(
        "--from-file",
        metavar="FILE",
        help="Read additional UUIDs from FILE (one per line; # comments allowed).",
    )
    parser.add_argument(
        "--output-dir",
        default="data/samples",
        metavar="DIR",
        help="Base dir holding the per-sample folders (default: ./data/samples).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download project/ even if it is already present.",
    )
    args = parser.parse_args()

    uuids = _collect_uuids(args)
    if not uuids:
        parser.error("no UUIDs given (pass them as arguments or via --from-file)")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    failures: list[tuple[str, str]] = []
    for uuid in uuids:
        try:
            download_one(uuid, output_dir, args.force)
        except Exception as e:  # noqa: BLE001 - report per-UUID and keep going
            print(f"  FAILED: {e}", file=sys.stderr)
            failures.append((uuid, str(e)))

    if failures:
        print(f"\n{len(failures)}/{len(uuids)} failed:", file=sys.stderr)
        for uuid, err in failures:
            print(f"  {uuid}: {err}", file=sys.stderr)
        sys.exit(1)
    print(f"\nAll {len(uuids)} sample(s) staged under {output_dir}/")


if __name__ == "__main__":
    main()
