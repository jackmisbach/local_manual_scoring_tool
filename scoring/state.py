"""Types for the scoring state shared between the CLI and the Flask app, plus
per-sample metadata loading from each sample dir's ``sample.json``."""

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict, cast

from scoring.store import TrackedSample

AdvanceResult = Literal["advanced", "exhausted", "broken"]

logger = logging.getLogger(__name__)

# Optional per-sample metadata file, living inside each sample dir.
SAMPLE_JSON_FILENAME = "sample.json"

# Default in-container mount target for the agent's project dir.
DEFAULT_PROJECT_DIR = "/home/agent/project"


QueueStatus = Literal[
    "open",
    "skipped",
    "scored",
    "done",
    "running",
    "ignored",
]

# Statuses "Next Sample" advances to. ``done`` and ``skipped`` are not navigable
# (the user already finished/skipped them); ``running``/``ignored`` are synthetic.
NAVIGABLE_STATUSES: frozenset[str] = frozenset({"open", "scored"})


class QueueEntry(TypedDict):
    """One sample in the scoring queue."""

    uuid: str
    task_name: str
    sample_id: str
    model: str
    status: QueueStatus
    score: NotRequired[float]
    explanation: NotRequired[str]
    minutes_to_score: NotRequired[float]
    has_stash: NotRequired[bool]
    error: NotRequired[str]


class BrokenSample(TypedDict):
    """A sample whose metadata could not be loaded; user must Skip or Exit."""

    uuid: str
    task_name: str
    error: str


class SetupState(TypedDict):
    """Live setup progress for the current sample."""

    status: str
    gui: NotRequired[bool]
    ready_at: NotRequired[float]
    # True once the server auto-launched VS Code into the ready container.
    attached: NotRequired[bool]
    message: NotRequired[str]
    # Declared deps that could not be installed; surfaced in the UI.
    dep_warnings: NotRequired[list[str]]
    queue: NotRequired[dict[str, int]]


class SampleInfo(TypedDict):
    """Scoring-relevant metadata for a single sample.

    Most run-info fields (tokens, durations, counts) have no local source and
    default to ``None``/``""``; the scoring page hides their rows when empty.
    ``task_name`` / ``task_variant`` / ``model`` / ``image`` come from the
    sample dir's ``sample.json`` when present.
    """

    task_name: str
    task_version: str
    task_variant: str
    project_dir: str
    image: str
    model: str
    created_by: str
    eval_set_id: str
    viewer_url: str
    started_at: str
    completed_at: str
    total_time_seconds: float | None
    working_time_seconds: float | None
    total_tokens: int | None
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    message_count: int | None
    action_count: int | None
    status: str
    # True when the sample has no ``sample.json``; the scoring page shows an
    # explanatory banner and suppresses metadata-derived UI.
    no_metadata: bool


class ScoringState(TypedDict):
    """Shared mutable state owned by the CLI and read/written by the Flask app."""

    mode: str
    queue: list[QueueEntry]
    current_idx: int
    uuid: str
    info: SampleInfo
    container_name: str
    setup_state: SetupState
    # The scorer's identity, written into every score's metadata.author and
    # shown on the scoring page.
    scorer_email: str
    samples_dir: Path
    images_dir: Path
    # The sample ids this run considers (a single id when pointed at one sample
    # dir, otherwise every discovered sample under samples_dir).
    sample_ids: list[str]
    cli_image: str
    gui_override: bool
    no_container: bool
    cli_no_container: bool
    attach: bool
    setup_fn: Callable[[], None]
    tracking: dict[str, TrackedSample]
    # UUIDs whose tracking row changed since the last save_tracking, so the
    # save only rewrites rows this process actually touched.
    tracking_touched: set[str]
    include_skipped: bool
    include_done: bool
    # Task universe for the Tasks… selection modal: every task name seen across
    # the sample dirs, plus per-task sample counts.
    all_tasks: list[str]
    task_counts: dict[str, int]
    pending_broken: NotRequired[BrokenSample]
    # Set when startup found nothing to score; the app redirects to /summary.
    pending_summary: NotRequired[bool]
    # Set when at least one task is unrecorded in the local selection file; the
    # app renders the Task Selection modal until the user saves.
    pending_task_selection: NotRequired[bool]
    # Set by /api/exit before spawning the exit daemon so late mutating POSTs
    # are rejected with 503.
    exiting: NotRequired[bool]


def sample_json_path(samples_dir: Path, uuid: str) -> Path:
    return samples_dir / uuid / SAMPLE_JSON_FILENAME


def load_sample_json(samples_dir: Path, uuid: str) -> dict[str, Any]:
    """Read a sample's ``sample.json``, or ``{}`` if missing/unreadable."""
    path = sample_json_path(samples_dir, uuid)
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Ignoring %s: %s", path, e)
        return {}
    if not isinstance(raw, dict):
        logger.warning("Ignoring %s: expected object, got %s", path, type(raw).__name__)
        return {}
    return cast(dict[str, Any], raw)


def sample_task_name(data: dict[str, Any]) -> str:
    """The task name from a ``sample.json`` dict (accepts ``task`` or ``task_name``)."""
    return str(data.get("task") or data.get("task_name") or "")


def local_sample_info(uuid: str, samples_dir: Path) -> SampleInfo:
    """Build a :class:`SampleInfo` from the sample dir's ``sample.json``.

    Recognized ``sample.json`` keys: ``task`` (task name), ``image`` (the image
    tarball filename inside the images dir), and optionally ``task_variant`` /
    ``model`` / ``project_dir``. Scoring works even with no ``sample.json`` (the
    form only needs the project dir and the guidance links); ``no_metadata``
    then drives an explanatory banner and the image must be supplied via
    ``--image``.
    """
    data = load_sample_json(samples_dir, uuid)
    return {
        "task_name": sample_task_name(data),
        "task_version": str(data.get("task_version", "") or ""),
        "task_variant": str(data.get("task_variant") or data.get("variant") or ""),
        "project_dir": str(data.get("project_dir", "") or DEFAULT_PROJECT_DIR),
        "image": str(data.get("image", "") or ""),
        "model": str(data.get("model", "") or ""),
        "created_by": str(data.get("created_by", "") or ""),
        "eval_set_id": str(data.get("eval_set_id", "") or ""),
        "viewer_url": "",
        "started_at": "",
        "completed_at": "",
        "total_time_seconds": None,
        "working_time_seconds": None,
        "total_tokens": None,
        "input_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "message_count": None,
        "action_count": None,
        "status": "",
        "no_metadata": not data,
    }


def stub_sample_info(entry: QueueEntry) -> SampleInfo:
    """Placeholder SampleInfo for the empty-queue case."""
    return {
        "task_name": entry.get("task_name", "") or "",
        "task_version": "",
        "task_variant": "",
        "project_dir": DEFAULT_PROJECT_DIR,
        "image": "",
        "model": entry.get("model", "") or "",
        "created_by": "",
        "eval_set_id": "",
        "viewer_url": "",
        "started_at": "",
        "completed_at": "",
        "total_time_seconds": None,
        "working_time_seconds": None,
        "total_tokens": None,
        "input_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "message_count": None,
        "action_count": None,
        "status": "",
        "no_metadata": True,
    }


def next_pending_index(queue: list[QueueEntry], after: int) -> int | None:
    """Return the index of the first navigable entry after ``after``, or None."""
    for i in range(after + 1, len(queue)):
        if queue[i]["status"] in NAVIGABLE_STATUSES:
            return i
    return None
