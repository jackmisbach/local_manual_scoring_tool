"""Local Docker container management, image loading, and VS Code attach.

This is the stripped-down infra layer: no Hawk API, no S3, no ECR. Task images
are staged on disk as ``docker save`` tarballs in an images dir
(``<images_dir>/<task_name>.tar``) and loaded on demand; project dirs are
already on disk and bind-mounted into the container.
"""

import json
import logging
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Every ScoreEvent appended by this tool uses the same scorer-process name —
# the human's identity lives in score.metadata.author, not here.
MANUAL_SCORE_SCORER = "manual_score"

# The seed-row scorer name carried in score metadata, kept for output
# compatibility with the upstream warehouse shape.
INFO_ROW = "manual_scorer"

# Every ScoreEvent copies INFO_ROW under this metadata key.
SCORER_NAME_METADATA_KEY = "scorer_name"

# Versions the scoring payload format (not the package).
SCORING_VERSION = "0.0.1"


def sample_needs_gui(sample_id: str) -> bool:
    """Auto-detect whether a sample needs GUI (noVNC) setup."""
    return "_gui_" in sample_id.lower() or sample_id.lower().endswith("_gui")


# ---------------------------------------------------------------------------
# Image loading (task tarballs)
# ---------------------------------------------------------------------------

_load_lock = threading.Lock()


def image_exists_locally(image: str) -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _image_tag_from_tarball(tar_path: Path) -> str | None:
    """Return the first RepoTag from a ``docker save`` tarball's manifest.json.

    Reads the manifest with the stdlib ``tarfile`` (no Docker call, no extra
    deps) so we can learn the image tag without loading the archive — which lets
    us skip the load entirely when the tag is already present locally. Returns
    None if the tarball is missing, unreadable, or tagged by ID only.
    """
    if not tar_path.exists():
        return None
    try:
        with tarfile.open(tar_path) as tf:
            member = tf.extractfile("manifest.json")
            if member is None:
                return None
            manifest = json.load(member)
    except (tarfile.TarError, OSError, json.JSONDecodeError) as e:
        logger.warning("Could not read manifest.json from %s: %s", tar_path, e)
        return None
    if isinstance(manifest, list) and manifest:
        tags = manifest[0].get("RepoTags") or []
        if tags:
            return str(tags[0])
    return None


def _docker_load(tar_path: Path) -> str | None:
    """``docker load`` ``tar_path``; return the loaded tag if Docker reports one."""
    result = subprocess.run(
        ["docker", "load", "-i", str(tar_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker load failed for {tar_path}: {result.stderr.strip()}")
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if line.startswith("Loaded image:"):
            return line.split(":", 1)[1].strip()
    return None


def ensure_task_image(
    images_dir: Path,
    image_file: str,
    *,
    cli_image: str | None = None,
) -> str:
    """Ensure the sample's image is loaded and return its Docker tag.

    - ``cli_image`` (the global ``--image`` override) takes precedence and must
      already be loaded locally.
    - Otherwise ``image_file`` is the image tarball's filename inside
      ``images_dir`` (from ``sample.json``'s ``image``). The tool reads the tag
      from the tarball's manifest; if that tag is already present locally it's
      reused (no reload — so samples sharing a tarball only load once),
      otherwise the tarball is ``docker load``ed.

    Raises ``RuntimeError`` with an actionable message on any failure.
    """
    if cli_image:
        if image_exists_locally(cli_image):
            return cli_image
        raise RuntimeError(
            f"--image '{cli_image}' is not loaded locally. `docker load`/`docker pull` it first."
        )

    if not image_file:
        raise RuntimeError(
            "No image for this sample: set sample.json 'image' to an image tarball filename"
            " in the images dir, or pass --image <local-docker-tag>."
        )

    tar_path = images_dir / image_file
    tag = _image_tag_from_tarball(tar_path)
    if tag and image_exists_locally(tag):
        return tag

    if not tar_path.exists():
        raise RuntimeError(
            f"Image tarball not found: {tar_path}. Stage it (e.g. with"
            " dev/download_starting_image.py or `docker save`) into the images dir."
        )

    with _load_lock:
        # Re-check under the lock: a concurrent setup may have just loaded it.
        if tag and image_exists_locally(tag):
            return tag
        loaded_tag = _docker_load(tar_path)
        tag = tag or loaded_tag

    if not tag:
        raise RuntimeError(
            f"Loaded {tar_path} but could not determine its image tag (tagged by ID only)."
        )
    if not image_exists_locally(tag):
        raise RuntimeError(f"Loaded {tar_path} but image '{tag}' is still not present locally.")
    return tag


# ---------------------------------------------------------------------------
# Project dir prep
# ---------------------------------------------------------------------------


def _disable_project_filemode(project_dir: Path) -> None:
    """Stop git from reporting spurious exec-bit changes on a staged project.

    The agent's repo was likely committed on Linux with executable bits; on a
    different host the worktree can read back as non-executable, making every
    tracked file show a phantom mode change. Setting core.fileMode=false in the
    pulled repo's own .git/config suppresses that, and rides the bind mount into
    the container which shares this file.
    """
    if not (project_dir / ".git").exists():
        return
    result = subprocess.run(
        ["git", "-C", str(project_dir), "config", "core.fileMode", "false"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"Warning: could not set core.fileMode in {project_dir}: {result.stderr.strip()}")


# ---------------------------------------------------------------------------
# Container lifecycle
# ---------------------------------------------------------------------------

_FORWARDED_PORTS = (3000, 5000, 6080, 8000, 8080, 8888)


def _enable_localhost_forwarding(container_name: str) -> None:
    """Start socat forwarders so servers bound to 127.0.0.1 are reachable.

    Agent servers typically bind to localhost, which Docker port-forwarding
    can't reach. A background socat per mapped port listens on 0.0.0.0 and
    relays to 127.0.0.1, making the server accessible from the host.
    """
    has_socat = subprocess.run(
        ["docker", "exec", container_name, "which", "socat"],
        capture_output=True,
        check=False,
    )
    if has_socat.returncode != 0:
        subprocess.run(
            [
                "docker",
                "exec",
                container_name,
                "bash",
                "-c",
                "apt-get update -qq && apt-get install -y -qq socat > /dev/null 2>&1",
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
    eth0_ip = subprocess.run(
        ["docker", "exec", container_name, "hostname", "-I"],
        capture_output=True,
        text=True,
        check=False,
    )
    bind_addr = eth0_ip.stdout.strip().split()[0] if eth0_ip.returncode == 0 else ""
    if not bind_addr:
        return
    for port in _FORWARDED_PORTS:
        subprocess.run(
            [
                "docker",
                "exec",
                "-d",
                container_name,
                "socat",
                f"TCP-LISTEN:{port},fork,bind={bind_addr},reuseaddr",
                f"TCP:127.0.0.1:{port}",
            ],
            capture_output=True,
            check=False,
        )


def start_container(
    image: str,
    container_name: str,
    sample_dir: Path,
    project_dir: str,
    gui: bool = False,
) -> tuple[str, list[str]]:
    """Start a detached container with the sample's ``project/`` bind-mounted.

    Returns (container_id, skipped_deps). ``project_dir`` is the in-container
    mount target (e.g. ``/home/agent/project``).
    """
    # Always remove any existing container to ensure fresh bind mounts.
    stop_container(container_name)

    mount_src = sample_dir / "project"
    if not mount_src.is_dir():
        mount_src = sample_dir / "artifacts"
    if not mount_src.is_dir():
        raise RuntimeError(
            f"Expected project files at {sample_dir / 'project'} (or"
            f" {sample_dir / 'artifacts'}) but neither directory exists."
            " Stage the agent's project/ dir inside this sample directory."
        )
    _disable_project_filemode(mount_src)

    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        container_name,
        "--platform",
        "linux/amd64",
    ]
    for port in _FORWARDED_PORTS:
        cmd += ["-p", f"127.0.0.1:{port}:{port}"]
    cmd += [
        "--init",
        "-w",
        "/home/agent",
        "-v",
        f"{mount_src.resolve()}:{project_dir}",
        image,
        "tail",
        "-f",
        "/dev/null",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Error starting container: {result.stderr.strip()}")

    container_id = result.stdout.strip()[:12]
    _enable_localhost_forwarding(container_name)
    skipped_deps = _install_project_deps(container_name, project_dir)
    if gui:
        setup_display(container_name)
    return container_id, skipped_deps


def _install_project_deps(container_name: str, project_dir: str) -> list[str]:
    """Install Python deps from requirements.txt if present.

    Returns packages that could not be installed. A single unbuildable package
    no longer aborts the whole install: the batch install is tried first, and on
    failure we fall back to a best-effort per-package install. Raises only if
    nothing could be installed at all.
    """
    has_req = subprocess.run(
        ["docker", "exec", container_name, "test", "-f", f"{project_dir}/requirements.txt"],
        capture_output=True,
        check=False,
    )
    if has_req.returncode != 0:
        return []

    result = subprocess.run(
        [
            "docker",
            "exec",
            container_name,
            "bash",
            "-c",
            f"pip install -q --disable-pip-version-check -r {project_dir}/requirements.txt",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode == 0:
        return []

    # Batch install is all-or-nothing; retry per-package so the rest still land.
    script = (
        'while IFS= read -r line || [ -n "$line" ]; do '
        'pkg="${line%%#*}"; pkg="$(echo "$pkg" | xargs)"; [ -z "$pkg" ] && continue; '
        'if pip install -q --disable-pip-version-check "$pkg"; then echo "__OK__ $pkg"; '
        'else echo "__SKIPPED__ $pkg"; fi; '
        f'done < "{project_dir}/requirements.txt"'
    )
    fb = subprocess.run(
        ["docker", "exec", container_name, "bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    lines = fb.stdout.splitlines()
    installed = [ln.split(None, 1)[1] for ln in lines if ln.startswith("__OK__")]
    skipped = [ln.split(None, 1)[1] for ln in lines if ln.startswith("__SKIPPED__")]
    if not installed:
        msg = (result.stdout + result.stderr).strip() or "unknown error"
        raise RuntimeError(f"pip install failed: {msg}")
    return skipped


def setup_display(container_name: str) -> bool:
    """Install and start Xvfb + noVNC so pygame apps can be viewed in a browser."""
    print("Setting up display (noVNC)...", flush=True)

    install = subprocess.run(
        [
            "docker",
            "exec",
            container_name,
            "bash",
            "-c",
            "DEBIAN_FRONTEND=noninteractive apt-get update -qq && "
            "apt-get install -y -qq --no-install-recommends xvfb x11vnc novnc websockify "
            "libsdl2-2.0-0 libsdl2-image-2.0-0 libsdl2-mixer-2.0-0 libsdl2-ttf-2.0-0 >/dev/null 2>&1",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if install.returncode != 0:
        print(f"noVNC install failed: {install.stderr.strip()}", file=sys.stderr)
        return False

    for cmd in [
        "Xvfb :99 -screen 0 1920x1080x24",
        "x11vnc -display :99 -forever -nopw -rfbport 5900",
    ]:
        subprocess.run(["docker", "exec", "-d", container_name, "bash", "-c", cmd], check=False)

    time.sleep(2)
    subprocess.run(
        [
            "docker",
            "exec",
            "-d",
            container_name,
            "bash",
            "-c",
            "websockify --web /usr/share/novnc 6080 localhost:5900",
        ],
        check=False,
    )
    time.sleep(1)

    verify = subprocess.run(
        [
            "docker",
            "exec",
            container_name,
            "python3",
            "-c",
            "import socket; s=socket.socket(); s.settimeout(2); s.connect(('localhost',5900)); s.close(); "
            "s=socket.socket(); s.settimeout(2); s.connect(('localhost',6080)); s.close()",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if verify.returncode == 0:
        print("noVNC display ready: http://localhost:6080/vnc.html")
        print(
            "Run GUI apps with: DISPLAY=:99 SDL_VIDEODRIVER=x11 XDG_RUNTIME_DIR=/tmp python main.py"
        )
        return True
    print(f"noVNC verification failed: {verify.stderr.strip()}", file=sys.stderr)
    return False


def stop_container(container_name: str) -> None:
    subprocess.run(["docker", "stop", container_name], capture_output=True, check=False)
    subprocess.run(["docker", "rm", container_name], capture_output=True, check=False)


def container_is_running(container_name: str) -> bool:
    """Check whether a container with this exact name is currently running."""
    result = subprocess.run(
        ["docker", "ps", "-q", "--filter", f"name=^{container_name}$"],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(result.stdout.strip())


def stop_all_scoring_containers() -> None:
    result = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "name=manual-score-"],
        capture_output=True,
        text=True,
        check=False,
    )
    ids = result.stdout.strip().split()
    if ids:
        subprocess.run(["docker", "stop", *ids], capture_output=True, check=False)
        subprocess.run(["docker", "rm", *ids], capture_output=True, check=False)


def list_running_scoring_containers() -> list[str]:
    """Names of currently-running ``manual-score-*`` containers."""
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}", "--filter", "name=manual-score-"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [n for n in result.stdout.splitlines() if n.startswith("manual-score-")]


def open_vscode_in_container(container_name: str, path: str = "/home/agent/project") -> None:
    code_path = shutil.which("code")
    if not code_path:
        return
    hex_name = container_name.encode().hex()
    uri = f"vscode-remote://attached-container+{hex_name}{path}"
    cmd = [code_path, "--folder-uri", uri]
    # On Windows the `code` launcher resolved by shutil.which is a .cmd batch
    # shim, which CreateProcess (subprocess without a shell) can't execute
    # directly — it raises WinError 193. Route it through cmd.exe so the shim
    # runs. (hex_name is hex and path is a fixed constant, so there's nothing
    # cmd could misinterpret.)
    if sys.platform == "win32":
        cmd = ["cmd", "/c", *cmd]
    try:
        subprocess.run(cmd, capture_output=True, check=False)
    except OSError as e:
        logger.info("VS Code launch skipped: %s", e)
