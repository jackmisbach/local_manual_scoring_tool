#!/usr/bin/env python3
"""CLI entry point for the local manual scoring tool.

Usage:
    local-manual-score [SAMPLES_DIR]

SAMPLES_DIR is a directory whose immediate subdirectories are samples (each a
``<uuid>/`` containing a ``project/`` dir). You can also point directly at a
single sample dir. Scores are written to ``<samples-dir>/scores.jsonl`` and
working/UI state to ``<samples-dir>/ui_state.jsonl``. Container images are
loaded on demand from ``<images-dir>/<task_name>.tar``.
"""

import argparse
import hashlib
import importlib
import logging
import random
import shutil
import socket
import sys
import threading
import time
from itertools import groupby
from pathlib import Path

from scoring.config import (
    config_main,
    ensure_defaults_persisted,
    open_url_in_browser,
    resolve_images_dir,
    resolve_samples_dir,
    resolve_scorer,
)
from scoring.infra import (
    ensure_task_image,
    open_vscode_in_container,
    sample_needs_gui,
    start_container,
    stop_all_scoring_containers,
    stop_container,
)
from scoring.state import (
    AdvanceResult,
    QueueEntry,
    QueueStatus,
    ScoringState,
    load_sample_json,
    local_sample_info,
    next_pending_index,
    sample_task_name,
    stub_sample_info,
)
from scoring.store import (
    STATUS_DONE,
    STATUS_OPEN,
    STATUS_SCORED,
    STATUS_SKIPPED,
    TrackedSample,
    derive_status_from_score_file,
    ensure_tracking_entries,
    load_score_file,
    load_tracking,
    queue_summary_fields,
    save_tracking,
)
from scoring.task_selection import load_selection

logger = logging.getLogger(__name__)

_SCORING_PORT = 9090


def port_in_use(port: int) -> bool:
    """True if anything's already listening on ``127.0.0.1:port``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def check_python_deps() -> None:
    """Import the app module up front so a broken install fails fast."""
    try:
        importlib.import_module("scoring.app")
    except ImportError as e:
        missing = e.name or str(e)
        print(f"Error: required Python package '{missing}' is missing.", file=sys.stderr)
        print("Sync dependencies from the repo root: uv sync", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Sample discovery + queue building
# ---------------------------------------------------------------------------


def has_project_dir(sample_dir: Path) -> bool:
    """A sample dir is one that holds the agent's ``project/`` (or ``artifacts/``)."""
    return (sample_dir / "project").is_dir() or (sample_dir / "artifacts").is_dir()


def discover_sample_ids(samples_dir: Path) -> list[str]:
    """Immediate subdirectories of ``samples_dir`` that contain a project dir."""
    if not samples_dir.is_dir():
        return []
    return [
        sub.name
        for sub in sorted(samples_dir.iterdir(), key=lambda p: p.name)
        if sub.is_dir() and has_project_dir(sub)
    ]


def shuffle_within_groups(queue: list[QueueEntry]) -> list[QueueEntry]:
    """Sort by (finished?, task_name), shuffle within each task group.

    Uses a deterministic seed derived from the set of ids so the order is stable
    across restarts for the same sample set.
    """
    digest = hashlib.sha256(",".join(sorted(e["uuid"] for e in queue)).encode()).digest()
    seed = int.from_bytes(digest[:8])

    def group_key(e: QueueEntry) -> tuple[int, str]:
        return (0 if e["status"] in ("done", "skipped") else 1, e["task_name"])

    queue.sort(key=group_key)
    result: list[QueueEntry] = []
    for _, group in groupby(queue, key=group_key):
        items = list(group)
        random.Random(seed).shuffle(items)
        result.extend(items)
    return result


def build_local_queue(
    samples_dir: Path,
    sample_ids: list[str],
    tracking: dict[str, TrackedSample],
    *,
    include_skipped: bool = False,
    include_done: bool = False,
    task_selection: dict[str, bool] | None = None,
) -> list[QueueEntry]:
    """Build the scoring queue from local sample dirs and the on-disk store.

    Status is derived from each sample's stored record. ``include_skipped`` /
    ``include_done`` re-surface skipped / finished samples as navigable. A
    ``task_selection`` drops samples whose (known) task is deselected.
    """
    queue: list[QueueEntry] = []
    for uuid in sample_ids:
        data = load_sample_json(samples_dir, uuid)
        task_name = sample_task_name(data)
        if (
            task_selection is not None
            and task_name
            and task_name in task_selection
            and not task_selection[task_name]
        ):
            continue
        tracking_entry = tracking.get(uuid, {})
        local_status = tracking_entry.get("status")
        score_file = load_score_file(samples_dir, uuid)
        summary_fields = queue_summary_fields(score_file)
        has_history = bool(score_file and score_file["history"])
        if local_status == STATUS_SKIPPED and not include_skipped:
            status: QueueStatus = "skipped"
        else:
            derived = derive_status_from_score_file(score_file, STATUS_OPEN)
            if derived == STATUS_DONE and include_done:
                derived = STATUS_SCORED if has_history else STATUS_OPEN
            status = derived
        entry: QueueEntry = {
            "uuid": uuid,
            "task_name": task_name or "(no task)",
            "sample_id": uuid,
            "model": str(data.get("model", "") or ""),
            "status": status,
            **summary_fields,  # type: ignore[typeddict-item]
        }
        queue.append(entry)
    return shuffle_within_groups(queue)


def scan_task_universe(samples_dir: Path, sample_ids: list[str]) -> tuple[list[str], dict[str, int]]:
    """Return (sorted task names, per-task sample counts) across the sample dirs."""
    counts: dict[str, int] = {}
    for uuid in sample_ids:
        tn = sample_task_name(load_sample_json(samples_dir, uuid))
        if not tn:
            continue
        counts[tn] = counts.get(tn, 0) + 1
    return sorted(counts), counts


def populate_tracking_from_queue(
    tracking: dict[str, TrackedSample],
    queue: list[QueueEntry],
    *,
    touched: set[str] | None = None,
) -> int:
    """Ensure every scorable queue id has a tracking row. Returns rows added."""
    by_id = {e["uuid"]: "" for e in queue if e["uuid"] not in tracking}
    return ensure_tracking_entries(tracking, by_id, touched=touched)


# ---------------------------------------------------------------------------
# Per-sample container setup (background thread)
# ---------------------------------------------------------------------------


def _update_setup_state(
    state: ScoringState,
    lock: threading.Lock,
    expected_uuid: str,
    *,
    status: str | None = None,
    ready_at: float | None = None,
    attached: bool | None = None,
    message: str | None = None,
    dep_warnings: list[str] | None = None,
) -> bool:
    """Update setup_state under lock; return False if the sample advanced."""
    with lock:
        if state["uuid"] != expected_uuid:
            return False
        if status is not None:
            state["setup_state"]["status"] = status
        if ready_at is not None:
            state["setup_state"]["ready_at"] = ready_at
        if attached is not None:
            state["setup_state"]["attached"] = attached
        if message is not None:
            state["setup_state"]["message"] = message
        if dep_warnings is not None:
            state["setup_state"]["dep_warnings"] = dep_warnings
        return True


def _setup_sample(state: ScoringState, lock: threading.Lock) -> None:
    """Background thread: load the task image and start the container."""
    uuid = ""
    try:
        with lock:
            uuid = state["uuid"]
            info = state["info"]
            container_name = state["container_name"]
            samples_dir = state["samples_dir"]
            images_dir = state["images_dir"]
            cli_image = state["cli_image"]
            no_container = state["no_container"]
            attach = state["attach"]
            sample_id = state["queue"][state["current_idx"]]["sample_id"]
            gui = state["gui_override"] or sample_needs_gui(sample_id)
            state["setup_state"]["gui"] = gui

        if no_container:
            _update_setup_state(state, lock, uuid, status="idle")
            return

        sample_dir = samples_dir / uuid

        # "pulling" is reused as the image-prep phase so the existing progress UI
        # shows a spinner; the message clarifies it's a local load.
        if not _update_setup_state(
            state, lock, uuid, status="pulling", message="Loading task image..."
        ):
            return
        try:
            image = ensure_task_image(
                images_dir,
                info.get("image", ""),
                cli_image=cli_image or None,
            )
        except RuntimeError as e:
            _update_setup_state(state, lock, uuid, status="error", message=str(e))
            return

        if not _update_setup_state(state, lock, uuid, status="starting"):
            return
        try:
            _, skipped_deps = start_container(
                image=image,
                container_name=container_name,
                sample_dir=sample_dir,
                project_dir=info["project_dir"],
                gui=gui,
            )
        except RuntimeError as e:
            _update_setup_state(state, lock, uuid, status="error", message=str(e))
            return

        attached = bool(attach and shutil.which("code"))
        if attached:
            open_vscode_in_container(container_name, info["project_dir"])
        _update_setup_state(
            state,
            lock,
            uuid,
            status="ready",
            ready_at=time.time(),
            attached=attached,
            dep_warnings=skipped_deps,
        )
        print("Ready to score — switch to the Scoring UI in your browser.")
    except Exception as e:  # noqa: BLE001 - surface any setup failure in the UI
        _update_setup_state(state, lock, uuid, status="error", message=str(e))


def advance_to_next(state: ScoringState, lock: threading.Lock) -> AdvanceResult:
    """Advance to the next pending sample and kick off its setup."""
    # Tear down first — the next container binds the same noVNC port (6080).
    stop_all_scoring_containers()

    with lock:
        state["no_container"] = state["cli_no_container"]
        next_idx = next_pending_index(state["queue"], state["current_idx"])
        if next_idx is None:
            return "exhausted"
        next_uuid = state["queue"][next_idx]["uuid"]
        samples_dir = state["samples_dir"]

    info = local_sample_info(next_uuid, samples_dir)

    with lock:
        state["current_idx"] = next_idx
        state["uuid"] = next_uuid
        state["info"] = info
        state["container_name"] = f"manual-score-{next_uuid[:12]}"
        state["setup_state"] = {"status": "starting"}

    threading.Thread(target=_setup_sample, args=(state, lock), daemon=True).start()
    return "advanced"


def save_state(state: ScoringState) -> None:
    """Persist the tracking rows touched since the last save."""
    touched = state["tracking_touched"]
    save_tracking(state["samples_dir"], state["tracking"], touched)
    touched.clear()


def rebuild_queue_after_selection(state: ScoringState, lock: threading.Lock) -> None:
    """Rebuild the queue with the current task selection; reset to first pending."""
    stop_all_scoring_containers()
    with lock:
        tracking = state["tracking"]
        touched = state["tracking_touched"]
        samples_dir = state["samples_dir"]
        sample_ids = list(state["sample_ids"])
        include_skipped = state["include_skipped"]
        include_done = state["include_done"]

    selection = load_selection()
    queue = build_local_queue(
        samples_dir,
        sample_ids,
        tracking,
        include_skipped=include_skipped,
        include_done=include_done,
        task_selection=selection or None,
    )
    current_idx = next((i for i, s in enumerate(queue) if s["status"] == "open"), 0)
    if queue:
        first_uuid = queue[current_idx]["uuid"]
        info = local_sample_info(first_uuid, samples_dir)
        container_name = f"manual-score-{first_uuid[:12]}"
        setup_status = "starting"
    else:
        first_uuid = ""
        info = stub_sample_info(
            {"uuid": "", "task_name": "", "sample_id": "", "model": "", "status": "open"}
        )
        container_name = ""
        setup_status = "idle"

    all_tasks, task_counts = scan_task_universe(samples_dir, sample_ids)

    with lock:
        state["queue"] = queue
        state["all_tasks"] = all_tasks
        state["task_counts"] = task_counts
        state["current_idx"] = current_idx
        state["uuid"] = first_uuid
        state["info"] = info
        state["container_name"] = container_name
        state["setup_state"] = {"status": setup_status}
        state.pop("pending_task_selection", None)
        populate_tracking_from_queue(tracking, queue, touched=touched)
        save_tracking(samples_dir, tracking, touched)
        touched.clear()

    if queue:
        threading.Thread(target=state["setup_fn"], daemon=True).start()


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def _resolve_targets(samples_dir: Path) -> tuple[Path, list[str]]:
    """Return (samples_root, sample_ids).

    If ``samples_dir`` itself holds a ``project/`` it's a single sample: the root
    becomes its parent and the queue is just that one id. Otherwise every
    discovered sample under ``samples_dir`` is included.
    """
    if has_project_dir(samples_dir):
        return samples_dir.parent, [samples_dir.name]
    return samples_dir, discover_sample_ids(samples_dir)


def _build_state(args: argparse.Namespace, scorer_email: str) -> ScoringState | None:
    single = has_project_dir(args.samples_dir)
    samples_root, sample_ids = _resolve_targets(args.samples_dir)
    images_dir = resolve_images_dir(args.images_dir, samples_root)

    if not sample_ids:
        print(
            f"No sample directories found in {samples_root}."
            " Each sample should be a subdirectory containing a project/ folder.",
            file=sys.stderr,
        )
        return None

    tracking = load_tracking(samples_root)
    selection = load_selection()
    queue = build_local_queue(
        samples_root,
        sample_ids,
        tracking,
        include_skipped=args.include_skipped,
        include_done=args.include_done,
        task_selection=selection or None,
    )
    if not queue:
        print(
            "All samples are filtered out by your task selection."
            " Use the Tasks button in the UI to re-enable tasks.",
            file=sys.stderr,
        )

    skipped = sum(1 for s in queue if s["status"] == "skipped")
    done = sum(1 for s in queue if s["status"] == "done")
    pending = sum(1 for s in queue if s["status"] in ("open", "scored"))
    no_pending = pending == 0
    if no_pending and queue:
        hints: list[str] = []
        if skipped:
            hints.append("--include-skipped")
        if done:
            hints.append("--include-done")
        hint = f" Use {' or '.join(hints)} to re-surface them." if hints else ""
        print(f"All {len(queue)} samples already processed.{hint} Opening summary.")
    elif queue:
        if skipped or done:
            print(f"Resuming: {done} done, {skipped} skipped, {pending} remaining of {len(queue)}")
        else:
            print(f"{len(queue)} sample(s) to score")

    current_idx = next((i for i, s in enumerate(queue) if s["status"] == "open"), 0)
    first_uuid = queue[current_idx]["uuid"] if queue else ""
    info = (
        local_sample_info(first_uuid, samples_root)
        if first_uuid
        else stub_sample_info(
            {"uuid": "", "task_name": "", "sample_id": "", "model": "", "status": "open"}
        )
    )
    all_tasks, task_counts = scan_task_universe(samples_root, sample_ids)

    state: ScoringState = {
        "mode": "single" if single else "queue",
        "queue": queue,
        "current_idx": current_idx,
        "uuid": first_uuid,
        "info": info,
        "container_name": f"manual-score-{first_uuid[:12]}" if first_uuid else "",
        "setup_state": {"status": "idle" if no_pending else "starting"},
        "scorer_email": scorer_email,
        "samples_dir": samples_root,
        "images_dir": images_dir,
        "sample_ids": sample_ids,
        "cli_image": args.image,
        "gui_override": args.gui,
        "no_container": args.no_container,
        "cli_no_container": args.no_container,
        "attach": not args.no_attach,
        "setup_fn": lambda: None,
        "tracking": tracking,
        "tracking_touched": set(),
        "include_skipped": args.include_skipped,
        "include_done": args.include_done,
        "all_tasks": all_tasks,
        "task_counts": task_counts,
    }
    if no_pending:
        state["pending_summary"] = True

    touched = state["tracking_touched"]
    populate_tracking_from_queue(tracking, queue, touched=touched)
    save_tracking(samples_root, tracking, touched)
    touched.clear()
    return state


def _run_app(args: argparse.Namespace, state: ScoringState, lock: threading.Lock) -> None:
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    state["setup_fn"] = lambda: _setup_sample(state, lock)

    if not state.get("pending_task_selection") and not state.get("pending_summary"):
        threading.Thread(target=state["setup_fn"], daemon=True).start()

    from scoring.app import create_app  # noqa: PLC0415

    app = create_app(
        state=state,
        lock=lock,
        advance_fn=lambda: advance_to_next(state, lock),
        save_fn=save_state,
        rebuild_queue_fn=lambda: rebuild_queue_after_selection(state, lock),
    )

    base_url = f"http://localhost:{args.port}"
    url = f"{base_url}/summary" if state.get("pending_summary") else base_url
    try:
        open_url_in_browser(url)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Open the URL manually.", file=sys.stderr)
    print(f"Scoring UI: {url}")

    try:
        app.run(host="127.0.0.1", port=args.port)
    except KeyboardInterrupt:
        pass
    finally:
        with lock:
            status = state["setup_state"].get("status", "")
            container_name = state["container_name"]
        if status in ("pulling", "starting", "ready"):
            print("\nStopping container...")
            stop_container(container_name)
        with lock:
            save_state(state)


def main() -> None:
    ensure_defaults_persisted()

    if len(sys.argv) > 1 and sys.argv[1] == "config":
        config_main(sys.argv[2:])
        return

    parser = argparse.ArgumentParser(
        description=(
            "Local web UI for manually scoring agent runs staged on disk.\n"
            "Run 'local-manual-score config --help' to inspect or modify saved config."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "samples_dir",
        nargs="?",
        type=Path,
        default=None,
        help=(
            "Directory of samples (each subdir a <uuid>/ with a project/ folder),"
            " or a single sample dir. Defaults to saved config, else ./samples."
        ),
    )
    parser.add_argument(
        "--images-dir",
        type=Path,
        default=None,
        help="Where task image tarballs (<task_name>.tar) live. Default: <samples-dir>/../images.",
    )
    parser.add_argument(
        "--image",
        default="",
        help="Override with an already-loaded local Docker tag for all samples"
        " (otherwise each sample.json's 'image' tarball is loaded from the images dir).",
    )
    parser.add_argument(
        "--port", type=int, default=_SCORING_PORT, help=f"Scoring UI port (default: {_SCORING_PORT})"
    )
    parser.add_argument(
        "--gui", action="store_true", help="Force noVNC for all samples (auto-detected for *_gui_*)"
    )
    parser.add_argument(
        "--no-container",
        action="store_true",
        help="Skip image load, container start, and VS Code launch (for UI testing).",
    )
    parser.add_argument(
        "--no-attach", action="store_true", help="Start containers but don't attach VS Code."
    )
    parser.add_argument(
        "--include-skipped", action="store_true", help="Re-surface previously skipped samples."
    )
    parser.add_argument(
        "--include-done", action="store_true", help="Re-surface samples marked done."
    )
    parser.add_argument(
        "--scorer", default="", help="Scorer identity (defaults to config / git user.email)."
    )
    args = parser.parse_args()

    check_python_deps()

    args.samples_dir = resolve_samples_dir(args.samples_dir)
    scorer_email = resolve_scorer(args.scorer)

    if port_in_use(args.port):
        print(
            f"local-manual-score is already running on port {args.port}."
            f" Open http://localhost:{args.port} or pass --port for a different one."
        )
        sys.exit(1)

    if not args.no_container and not shutil.which("docker"):
        print(
            "Error: 'docker' not found in PATH. Install Docker Desktop, or pass"
            " --no-container to run the UI without containers.",
            file=sys.stderr,
        )
        sys.exit(1)

    lock = threading.Lock()
    state = _build_state(args, scorer_email)
    if state is None:
        return
    _run_app(args, state, lock)


if __name__ == "__main__":
    main()
