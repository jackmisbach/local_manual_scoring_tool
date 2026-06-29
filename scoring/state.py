"""Types for the scoring state shared between the CLI and the Flask app, plus
per-sample metadata loading from each sample dir's ``sample.json``.
"""

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict, cast

AdvanceResult = Literal["advanced", "exhausted"]

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
]

# Statuses "Next Sample" advances to. ``done`` and ``skipped`` are not navigable
# (the user already finished/skipped them).
NAVIGABLE_STATUSES: frozenset[str] = frozenset({"open", "scored"})


class QueueEntry(TypedDict):
    """One sample in the scoring queue."""

    uuid: str
    task_name: str
    status: QueueStatus
    score: NotRequired[float]
    explanation: NotRequired[str]
    minutes_to_score: NotRequired[float]
    has_stash: NotRequired[bool]


class SetupState(TypedDict):
    """Live setup progress for the current sample."""

    status: str
    ready_at: NotRequired[float]
    # True once the server auto-launched VS Code into the ready container.
    attached: NotRequired[bool]
    message: NotRequired[str]
    # Declared deps that could not be installed; surfaced in the UI.
    dep_warnings: NotRequired[list[str]]
    queue: NotRequired[dict[str, int]]


class SampleInfo(TypedDict):
    """Scoring-relevant metadata for a single sample, read from ``sample.json``.

    Scoring works even with no ``sample.json`` (the form only needs the project
    dir and the guidance links); ``no_metadata`` then drives an explanatory
    banner and the image must be supplied via ``--image``.
    """

    task_name: str
    task_version: str
    task_variant: str
    project_dir: str
    image: str
    # URL of the task-specific scoring instructions doc (from sample.json).
    scoring_instructions_url: str
    # True when the sample has no ``sample.json``; the scoring page shows an
    # explanatory banner and suppresses metadata-derived UI.
    no_metadata: bool


class ScoringState(TypedDict):
    """Shared mutable state owned by the CLI and read/written by the Flask app."""

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
    no_container: bool
    cli_no_container: bool
    attach: bool
    setup_fn: Callable[[], None]
    # In-memory mirror of the persisted ``skipped`` flags, consulted when
    # deriving a sample's queue status (see ``derive_queue_status``).
    skipped: set[str]
    include_skipped: bool
    include_done: bool
    # Set when startup found nothing to score; the app redirects to /summary.
    pending_summary: NotRequired[bool]
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


def _sample_info(data: dict[str, Any]) -> SampleInfo:
    """Build a :class:`SampleInfo` from a ``sample.json`` dict (``{}`` if none).

    The sample's canonical identifier is its dir name (the ``uuid`` the whole
    tool keys on), tracked separately on the queue/state — it is not a
    ``SampleInfo`` field.
    """
    return {
        "task_name": sample_task_name(data),
        "task_version": str(data.get("task_version", "") or ""),
        "task_variant": str(data.get("task_variant") or data.get("variant") or ""),
        "project_dir": str(data.get("project_dir", "") or DEFAULT_PROJECT_DIR),
        "image": str(data.get("image", "") or ""),
        "scoring_instructions_url": str(data.get("scoring_instructions_url", "") or ""),
        "no_metadata": not data,
    }


def local_sample_info(uuid: str, samples_dir: Path) -> SampleInfo:
    """Build a :class:`SampleInfo` from the sample dir's ``sample.json``.

    Recognized ``sample.json`` keys: ``task`` (task name), ``image`` (the image
    tarball filename inside the images dir), and optionally ``task_variant`` /
    ``project_dir`` / ``scoring_instructions_url`` (the task-specific scoring
    instructions doc URL). The sample's identifier is the dir name (``uuid``), not a
    ``sample.json`` field. Scoring works even with no ``sample.json`` (the form
    only needs the project dir and the guidance links); ``no_metadata`` then
    drives an explanatory banner and the image must be supplied via ``--image``.
    """
    return _sample_info(load_sample_json(samples_dir, uuid))


def stub_sample_info() -> SampleInfo:
    """Placeholder SampleInfo for the empty-queue case (no sample to load)."""
    return _sample_info({})


def next_pending_index(queue: list[QueueEntry], after: int) -> int | None:
    """Return the index of the first navigable entry after ``after``, or None."""
    for i in range(after + 1, len(queue)):
        if queue[i]["status"] in NAVIGABLE_STATUSES:
            return i
    return None
