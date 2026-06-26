#!/usr/bin/env python3
"""Download the unique starting Docker images for one or more run (sample) UUIDs.

For each sample UUID this resolves the task name + version via the Hawk API to
its ECR task image, dedupes across all UUIDs (many samples share a task+version),
then pulls each unique image and saves it via ``docker save`` to a file named by
task + version, with ``.`` replaced by ``_`` — e.g. the image
``...:sunlight-0.1.0`` is written to ``sunlight-0_1_0.tar``. Load it on another
machine with ``docker load -i sunlight-0_1_0.tar``.

This is a *dev* utility: the stripped-down manual scoring tool assumes project
dirs and images are already staged on disk, so this script is how you stage
them. It mirrors the resolution / ECR-auth flow in METR/manual-scoring-tool's
``scoring/infra.py`` but is self-contained (stdlib + the ``hawk``, ``aws``, and
``docker`` CLIs; shared Hawk helpers live in ``_common.py``).

Tarballs are written to the scoring tool's images dir, ``data/images/`` by
default (override with --output-dir). Each scored sample's ``sample.json``
``image`` field must equal the tarball **filename** produced here (e.g.
``sunlight-0_1_0.tar``) — that's how the tool finds the image for a sample.

Prerequisites (same as manual-scoring-tool):
  - ``hawk`` CLI, logged in (``hawk login``)
  - ``aws`` CLI with the TaskDeveloper SSO role (``aws sso login``)
  - Docker Desktop running

Usage:
  ./download_starting_image.py <uuid> [<uuid> ...]
  ./download_starting_image.py --from-file uuids.txt
  ./download_starting_image.py <uuid> --output-dir ./data/images --force-pull
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from _common import ECR_REGISTRY, ECR_TASKS_REPO, resolve_sample, tar_name

_ecr_config_dir: Path | None = None


# ---------------------------------------------------------------------------
# ECR auth, pull, save
# ---------------------------------------------------------------------------


def _parse_ecr_region(image_name: str) -> str:
    """Extract the AWS region from an ECR image URL.

    Format: {account}.dkr.ecr.{region}.amazonaws.com/...
    """
    return image_name.split(".")[3]


def ensure_ecr_auth() -> None:
    """Verify AWS credentials and stage an inlined Docker auth config for ECR.

    Writes a temporary Docker config with the ECR token inlined (rather than
    running ``docker login``, which can fail on some credential stores) and
    records its dir in ``_ecr_config_dir`` for ``pull_image`` to pass via
    ``--config``.
    """
    global _ecr_config_dir
    profile = os.environ.get("AWS_PROFILE", "")
    hint = f"  export AWS_PROFILE={profile}\n  aws sso login" if profile else "  aws sso login"

    result = subprocess.run(["aws", "sts", "get-caller-identity"], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"AWS credentials are not valid. Run:\n{hint}")

    region = _parse_ecr_region(ECR_TASKS_REPO)
    password = subprocess.run(
        ["aws", "ecr", "get-login-password", "--region", region],
        capture_output=True,
        text=True,
    )
    if password.returncode != 0:
        raise RuntimeError(f"Failed to get ECR login password. Run:\n{hint}")

    token = base64.b64encode(f"AWS:{password.stdout.strip()}".encode()).decode()
    config = {"auths": {ECR_REGISTRY: {"auth": token}}}
    config_dir = Path(tempfile.gettempdir()) / "manual-scoring-ecr"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps(config))
    _ecr_config_dir = config_dir


def image_exists_locally(image: str) -> bool:
    result = subprocess.run(["docker", "image", "inspect", image], capture_output=True, text=True)
    return result.returncode == 0


def pull_image(image: str, force: bool = False) -> None:
    """Pull ``image`` from ECR (skipping if already local, unless ``force``)."""
    if not force and image_exists_locally(image):
        print(f"  image already present locally: {image}")
        return
    ensure_ecr_auth()
    cmd = ["docker"]
    if _ecr_config_dir is not None:
        cmd += ["--config", str(_ecr_config_dir)]
    cmd += ["pull", "--platform", "linux/amd64", image]
    print(f"  pulling {image} ...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Error pulling image: {result.stderr.strip()}")


def save_image(image: str, out_path: Path) -> None:
    """``docker save`` ``image`` to ``out_path``."""
    print(f"  saving to {out_path} ...")
    result = subprocess.run(
        ["docker", "save", "-o", str(out_path), image],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Error saving image: {result.stderr.strip()}")


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
        description="Download the starting Docker image(s) for one or more run/sample UUIDs.",
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
        default="data/images",
        metavar="DIR",
        help="Where to write the <task>-<version>.tar files, i.e. the scoring"
        " tool's images dir (default: ./data/images).",
    )
    parser.add_argument(
        "--force-pull",
        action="store_true",
        help="Re-pull the image even if it is already present locally.",
    )
    args = parser.parse_args()

    uuids = _collect_uuids(args)
    if not uuids:
        parser.error("no UUIDs given (pass them as arguments or via --from-file)")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    failures: list[tuple[str, str]] = []

    # Resolve every UUID to its starting image first, then dedupe so each
    # unique image is pulled/saved exactly once (multiple samples often share
    # the same task+version).
    image_to_uuids: dict[str, list[str]] = {}
    for uuid in uuids:
        try:
            image = resolve_sample(uuid)["image"]
        except Exception as e:  # noqa: BLE001 - report per-UUID and keep going
            print(f"{uuid}: FAILED to resolve: {e}", file=sys.stderr)
            failures.append((uuid, str(e)))
            continue
        print(f"{uuid} -> {image}")
        image_to_uuids.setdefault(image, []).append(uuid)

    print(f"\n{len(image_to_uuids)} unique image(s) from {len(uuids)} UUID(s).")

    saved_tarnames: list[str] = []
    for image, src_uuids in image_to_uuids.items():
        tarname = tar_name(image)
        out_path = output_dir / tarname
        print(f"\n{image}  (from {len(src_uuids)} sample(s)) -> {tarname}")
        try:
            pull_image(image, force=args.force_pull)
            save_image(image, out_path)
            size_mb = out_path.stat().st_size / 1024**2
            print(f"  done: {out_path} ({size_mb:.0f} MB)")
            saved_tarnames.append(tarname)
        except Exception as e:  # noqa: BLE001 - report per-image and keep going
            print(f"  FAILED: {e}", file=sys.stderr)
            failures.append((image, str(e)))

    if failures:
        print(f"\n{len(failures)} failure(s):", file=sys.stderr)
        for ref, err in failures:
            print(f"  {ref}: {err}", file=sys.stderr)
        sys.exit(1)
    print(f"\nAll {len(image_to_uuids)} unique image(s) saved to {output_dir}/")
    print(
        "\nReminder: set each scored sample's sample.json 'image' field to the"
        " matching tarball filename above — that's how the scoring tool resolves"
        " a sample's image, e.g.:"
    )
    for tarname in saved_tarnames:
        print(f'  "image": "{tarname}"')


if __name__ == "__main__":
    main()
