"""Local on-disk store for the manual scoring tool.

Two JSONL files live at the samples root:

- ``ui_state.jsonl`` — one line per sample, keyed by sample ``id`` (the sample
  dir name). Each record holds both the sample's tracking state (``status``,
  ``updated_at``, optional ``stashed``) and its working/UI state (``history``,
  ``stash``, ``finish_events``, ``scratchpad``, ``timers``).
  This single keyed store replaces the upstream tool's per-sample ``_score.json``
  files plus the root ``tracking.json``. Updates are read-modify-rewrite under a
  module lock.

- ``scores.jsonl`` — append-only canonical export. One flat line per finalized
  score submission (``id``, ``scorer``, ``value``, ``explanation``,
  ``minutes_to_score``, ``scored_at``, ``task_name``, ``model``).

A third small file, ``.task_scratchpads.json``, keeps reusable per-task notes
(keyed by task name), seeded into a sample's scratch pad when you open it.

Status taxonomy (shared between the store and the in-memory queue):

- ``open`` — sample is set up for scoring but the user hasn't acted yet.
- ``scored`` — at least one history entry exists.
- ``pending_score`` / ``pending_done`` — kept for compatibility with the
  upstream tracking model; in local mode every entry is born ``submitted_to:
  "local"`` so a finalized sample reads as ``scored`` (or ``done`` once the user
  clicks "Done Scoring").
- ``done`` — user clicked "Done Scoring This Sample".
- ``skipped`` — user actively skipped.

Two extra values exist only on synthetic queue entries (never persisted):
``running`` and ``ignored``.
"""

import json
import logging
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict, cast

logger = logging.getLogger(__name__)

UI_STATE_FILENAME = "ui_state.jsonl"
SCORES_FILENAME = "scores.jsonl"
TASK_SCRATCHPADS_FILENAME = ".task_scratchpads.json"

Status = Literal["open", "skipped", "scored", "done"]

STATUS_OPEN: Status = "open"
STATUS_SKIPPED: Status = "skipped"
STATUS_SCORED: Status = "scored"
STATUS_DONE: Status = "done"


# Banner rows for the prior-submission display (queue summary + per-sample
# panel). Each tuple is (label, metadata key) — read from ``score.metadata``
# on local history entries.
EXISTING_METADATA_DISPLAY: tuple[tuple[str, str], ...] = (
    ("Minutes spent scoring", "minutes_spent_scoring"),
)


class TrackedSample(TypedDict):
    """The tracking subset of a ``ui_state.jsonl`` record.

    ``stashed`` is omitted (not written as ``false``) when the sample has no
    in-progress draft — consumers should treat absence as ``False``.
    """

    status: Status
    eval_set_id: str
    updated_at: str
    stashed: NotRequired[bool]


class ScoreFormData(TypedDict, total=False):
    """Form-level view of a score — value, explanation, minutes.

    Lives only in transit (form POST -> ``form_to_score_event``); never
    persisted. See :class:`ScoreEventScore` for the on-disk shape.
    """

    value: float
    explanation: str
    minutes_to_score: float


class ScoreEventScore(TypedDict, total=False):
    """The ``score`` payload of a scoring event (mirrors inspect_ai's Score)."""

    value: float
    answer: None
    explanation: str
    history: list[Any]
    metadata: dict[str, Any]


class SavedScore(TypedDict, total=False):
    """One scoring event — either the current stash draft or a history entry.

    ``score`` is the inspect_ai Score payload (value, explanation, metadata with
    author, minutes_spent_scoring, etc.); ``scorer`` is the constant scorer
    name — the human's identity lives in ``score.metadata.author``. ``saved_at``
    is the ISO 8601 timestamp of the local persist.
    """

    scorer: str
    score: ScoreEventScore
    saved_at: NotRequired[str]


class FinishRecord(TypedDict, total=False):
    """One "done scoring" record for a sample."""

    author: str
    sent_at: str


class TimerSnapshot(TypedDict, total=False):
    """Last known state of one stopwatch timer."""

    elapsed_seconds: float


class TimerState(TypedDict, total=False):
    """Per-sample UI-only timer state."""

    normal: TimerSnapshot


class ScoreFile(TypedDict):
    """The working/UI subset of a ``ui_state.jsonl`` record.

    Named ``ScoreFile`` for continuity with the call sites that consume it:
    - ``stash`` (optional): a single in-progress draft.
    - ``history``: append-only list of finalized scoring events.
    - ``finish_events`` (optional): "Done Scoring This Sample" records.
    - ``scratchpad`` (optional): free-form notes for the sample.
    - ``timers`` (optional): persisted stopwatch state.
    """

    stash: NotRequired[SavedScore]
    history: list[SavedScore]
    finish_events: NotRequired[list[FinishRecord]]
    scratchpad: NotRequired[str]
    timers: NotRequired[TimerState]


# Keys of a record that belong to the ScoreFile (working/UI) view. Everything
# else in a record (status, eval_set_id, updated_at, stashed) is the tracking view.
_SCORE_KEYS: frozenset[str] = frozenset(
    {"history", "stash", "finish_events", "scratchpad", "timers"}
)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Record I/O (ui_state.jsonl)
# ---------------------------------------------------------------------------

# Serializes the read-modify-rewrite on ui_state.jsonl so concurrent writers
# (the background setup thread, timer-state autosaves, the submit handler)
# can't clobber each other.
_records_lock = threading.RLock()


def ui_state_path(root: Path) -> Path:
    return root / UI_STATE_FILENAME


def scores_path(root: Path) -> Path:
    return root / SCORES_FILENAME


def _load_records(root: Path) -> dict[str, dict[str, Any]]:
    """Read ``ui_state.jsonl`` into ``{id: record}``; ``{}`` if missing.

    Malformed lines are skipped (with a warning) rather than aborting the load,
    so a single bad hand-edit can't take down the scoring page.
    """
    path = ui_state_path(root)
    if not path.exists():
        return {}
    records: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("Ignoring %s line %d: %s", path, lineno, e)
                continue
            if not isinstance(rec, dict) or "id" not in rec:
                logger.warning("Ignoring %s line %d: not a record with an id", path, lineno)
                continue
            records[str(rec["id"])] = rec
    return records


def _write_records(root: Path, records: dict[str, dict[str, Any]]) -> None:
    """Rewrite ``ui_state.jsonl`` from ``{id: record}``, one line per sample."""
    path = ui_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for sample_id in sorted(records):
            rec = dict(records[sample_id])
            rec["id"] = sample_id
            f.write(json.dumps(rec, sort_keys=True))
            f.write("\n")


def _ensure_record(records: dict[str, dict[str, Any]], sample_id: str) -> dict[str, Any]:
    """Return the record for ``sample_id``, seeding an ``open`` tracking row."""
    rec = records.get(sample_id)
    if rec is None:
        rec = {"id": sample_id, "status": STATUS_OPEN, "eval_set_id": "", "updated_at": now_iso()}
        records[sample_id] = rec
    return rec


# ---------------------------------------------------------------------------
# Tracking view
# ---------------------------------------------------------------------------


def load_tracking(root: Path) -> dict[str, TrackedSample]:
    """Return ``{id: TrackedSample}`` from the store."""
    out: dict[str, TrackedSample] = {}
    for sample_id, rec in _load_records(root).items():
        entry: TrackedSample = {
            "status": cast(Status, rec.get("status", STATUS_OPEN)),
            "eval_set_id": rec.get("eval_set_id", "") or "",
            "updated_at": rec.get("updated_at", "") or "",
        }
        if rec.get("stashed"):
            entry["stashed"] = True
        out[sample_id] = entry
    return out


def save_tracking(
    root: Path,
    tracking: dict[str, TrackedSample],
    touched_uuids: Iterable[str],
) -> None:
    """Persist the tracking fields of ``touched_uuids`` into ``ui_state.jsonl``.

    Re-reads the file, overlays the tracking subset for the listed ids
    (preserving each record's score/UI fields), and writes back. Records this
    process didn't touch survive untouched.
    """
    with _records_lock:
        records = _load_records(root)
        for sample_id in touched_uuids:
            entry = tracking.get(sample_id)
            if entry is None:
                continue
            rec = _ensure_record(records, sample_id)
            rec["status"] = entry["status"]
            rec["eval_set_id"] = entry.get("eval_set_id", "") or ""
            rec["updated_at"] = entry.get("updated_at", "") or now_iso()
            if entry.get("stashed"):
                rec["stashed"] = True
            else:
                rec.pop("stashed", None)
        _write_records(root, records)


def set_status(
    tracking: dict[str, TrackedSample],
    uuid: str,
    status: Status,
    eval_set_id: str = "",
    *,
    touched: set[str] | None = None,
) -> None:
    """Upsert an in-memory tracking entry, preserving the ``stashed`` flag."""
    existing = tracking.get(uuid)
    resolved_eval_set = eval_set_id or (existing["eval_set_id"] if existing else "")
    entry: TrackedSample = {
        "status": status,
        "eval_set_id": resolved_eval_set,
        "updated_at": now_iso(),
    }
    if existing is not None and existing.get("stashed"):
        entry["stashed"] = True
    tracking[uuid] = entry
    if touched is not None:
        touched.add(uuid)


def set_stashed(
    tracking: dict[str, TrackedSample],
    uuid: str,
    stashed: bool,
    eval_set_id: str = "",
    *,
    touched: set[str] | None = None,
) -> None:
    """Flip the in-memory ``stashed`` flag without changing the status."""
    existing = tracking.get(uuid)
    if existing is None:
        tracking[uuid] = {
            "status": STATUS_OPEN,
            "eval_set_id": eval_set_id,
            "updated_at": now_iso(),
        }
    entry: TrackedSample = dict(tracking[uuid])  # type: ignore[typeddict-item]
    if eval_set_id:
        entry["eval_set_id"] = eval_set_id
    entry["updated_at"] = now_iso()
    if stashed:
        entry["stashed"] = True
    else:
        entry.pop("stashed", None)
    tracking[uuid] = entry
    if touched is not None:
        touched.add(uuid)


def ensure_tracking_entries(
    tracking: dict[str, TrackedSample],
    uuids_by_eval_set: dict[str, str],
    *,
    touched: set[str] | None = None,
) -> int:
    """Create ``open`` rows for any queue uuids missing from the in-memory map."""
    added = 0
    for uuid, eval_set_id in uuids_by_eval_set.items():
        if uuid in tracking:
            continue
        tracking[uuid] = {
            "status": STATUS_OPEN,
            "eval_set_id": eval_set_id,
            "updated_at": now_iso(),
        }
        added += 1
        if touched is not None:
            touched.add(uuid)
    return added


# ---------------------------------------------------------------------------
# Score-file (working/UI) view
# ---------------------------------------------------------------------------


def load_score_file(root: Path, uuid: str) -> ScoreFile | None:
    """Read the working/UI state for ``uuid``.

    Returns ``None`` when the sample has no record, or only a bare tracking row
    with no score/UI content (mirrors the upstream "missing _score.json" case).
    """
    rec = _load_records(root).get(uuid)
    if rec is None:
        return None
    if not any(k in rec for k in _SCORE_KEYS):
        return None
    raw: dict[str, Any] = {k: rec[k] for k in _SCORE_KEYS if k in rec}
    if not isinstance(raw.get("history"), list):
        raw["history"] = []
    return cast(ScoreFile, raw)


def write_score_file(root: Path, uuid: str, score_file: ScoreFile) -> None:
    """Replace the score/UI portion of ``uuid``'s record with ``score_file``.

    Tracking fields (status, eval_set_id, updated_at, stashed) are preserved;
    score keys absent from ``score_file`` are removed (so e.g. clearing the
    stash drops it).
    """
    with _records_lock:
        records = _load_records(root)
        rec = _ensure_record(records, uuid)
        for key in _SCORE_KEYS:
            rec.pop(key, None)
        for key, value in score_file.items():
            rec[key] = value
        _write_records(root, records)


def save_stash(root: Path, uuid: str, draft: SavedScore) -> None:
    """Update the stash slot, preserving the existing history list."""
    existing: ScoreFile = load_score_file(root, uuid) or {"history": []}
    existing["stash"] = {**draft, "saved_at": now_iso()}
    write_score_file(root, uuid, existing)


def clear_stash(root: Path, uuid: str) -> ScoreFile | None:
    """Drop the in-progress stash, leaving history intact."""
    score_file = load_score_file(root, uuid)
    if score_file is None:
        return None
    if score_file.pop("stash", None) is None:
        return score_file
    write_score_file(root, uuid, score_file)
    return score_file


def append_history(
    root: Path, uuid: str, entry: SavedScore, *, drop_stash: bool = False
) -> None:
    """Append a finalized scoring event to ``history``.

    Stamps ``saved_at``. ``drop_stash`` removes the in-progress draft as part of
    the same write.
    """
    score_file: ScoreFile = load_score_file(root, uuid) or {"history": []}
    stamped: SavedScore = {**entry, "saved_at": now_iso()}
    score_file["history"].append(stamped)
    if drop_stash:
        score_file.pop("stash", None)
    write_score_file(root, uuid, score_file)


def append_finish_event(root: Path, uuid: str, author: str) -> None:
    """Record a "Done Scoring This Sample" event inside the sample's record."""
    score_file: ScoreFile = load_score_file(root, uuid) or {"history": []}
    record: FinishRecord = {"author": author, "sent_at": now_iso()}
    events = list(score_file.get("finish_events") or [])
    events.append(record)
    score_file["finish_events"] = events
    write_score_file(root, uuid, score_file)


def clear_finish_events(root: Path, uuid: str) -> None:
    """Remove all finish events so the sample can be re-scored."""
    score_file: ScoreFile = load_score_file(root, uuid) or {"history": []}
    score_file.pop("finish_events", None)
    write_score_file(root, uuid, score_file)


def load_scratchpad(root: Path, uuid: str) -> str:
    """Return the saved scratch-pad text, or "" if absent."""
    score_file = load_score_file(root, uuid)
    if score_file is None:
        return ""
    return score_file.get("scratchpad") or ""


def save_scratchpad(root: Path, uuid: str, content: str) -> None:
    """Upsert (or clear) scratch-pad text inside the sample's record."""
    score_file: ScoreFile = load_score_file(root, uuid) or {"history": []}
    if content:
        score_file["scratchpad"] = content
    else:
        score_file.pop("scratchpad", None)
    write_score_file(root, uuid, score_file)


def load_timer_state(root: Path, uuid: str) -> TimerState | None:
    """Return the persisted per-sample timer state, or None if never saved."""
    score_file = load_score_file(root, uuid)
    if score_file is None:
        return None
    return score_file.get("timers")


def save_timer_state(root: Path, uuid: str, state: TimerState | None) -> None:
    """Upsert (or clear) the per-sample timer state."""
    score_file: ScoreFile = load_score_file(root, uuid) or {"history": []}
    if state:
        score_file["timers"] = state
    else:
        score_file.pop("timers", None)
    write_score_file(root, uuid, score_file)


# ---------------------------------------------------------------------------
# Canonical scores export (scores.jsonl)
# ---------------------------------------------------------------------------

_scores_lock = threading.Lock()


def score_path(root: Path) -> Path:
    """Filesystem path of the canonical scores export."""
    return scores_path(root)


def append_score_jsonl(root: Path, uuid: str, entry: SavedScore, info: dict[str, Any]) -> Path:
    """Append one flat finalized-score line to ``scores.jsonl``.

    ``entry`` is the SavedScore just appended to history; ``info`` is the
    sample's metadata (task_name, model). Returns the path written to.
    """
    score = entry.get("score") or {}
    meta = score.get("metadata") or {}
    row = {
        "id": uuid,
        "scorer": meta.get("author") or entry.get("scorer") or "",
        "value": score.get("value"),
        "explanation": score.get("explanation", ""),
        "minutes_to_score": meta.get("minutes_spent_scoring"),
        "scored_at": meta.get("scored_at") or now_iso(),
        "task_name": info.get("task_name", "") or "",
        "model": info.get("model", "") or "",
    }
    path = scores_path(root)
    with _scores_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True))
            f.write("\n")
    return path


# ---------------------------------------------------------------------------
# Reusable per-task scratch pads (.task_scratchpads.json)
# ---------------------------------------------------------------------------


class TaskScratchpadEntry(TypedDict):
    """Reusable scratch-pad notes shared across all samples of one task."""

    task: NotRequired[str]
    variants: NotRequired[dict[str, str]]


# task_name -> reusable notes.
TaskScratchpads = dict[str, TaskScratchpadEntry]


def task_scratchpads_path(root: Path) -> Path:
    return root / TASK_SCRATCHPADS_FILENAME


def clean_scratchpad_entry(entry: object) -> TaskScratchpadEntry | None:
    """Coerce one raw store value into a TaskScratchpadEntry, or None if unusable."""
    if not isinstance(entry, dict):
        return None
    clean: TaskScratchpadEntry = {}
    task = entry.get("task")
    if isinstance(task, str) and task:
        clean["task"] = task
    raw_variants = entry.get("variants")
    if isinstance(raw_variants, dict):
        variants = {
            name: note
            for name, note in raw_variants.items()
            if isinstance(name, str) and isinstance(note, str) and note
        }
        if variants:
            clean["variants"] = variants
    return clean


def load_task_scratchpads(root: Path) -> TaskScratchpads:
    """Read the task/variant scratch-pad store, or ``{}`` if missing/unreadable."""
    path = task_scratchpads_path(root)
    if not path.exists():
        return {}
    try:
        with path.open() as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Ignoring %s: %s", path, e)
        return {}
    if not isinstance(raw, dict):
        logger.warning("Ignoring %s: expected object, got %s", path, type(raw).__name__)
        return {}
    store: TaskScratchpads = {}
    for task_name, entry in raw.items():
        clean = clean_scratchpad_entry(entry)
        if clean is None:
            logger.warning("Ignoring malformed scratch-pad entry %r in %s", task_name, path)
            continue
        store[task_name] = clean
    return store


def write_task_scratchpads(root: Path, store: TaskScratchpads) -> None:
    path = task_scratchpads_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(store, f, indent=2, sort_keys=True)


def load_task_scratchpad(root: Path, task_name: str) -> str:
    """Return the generic task-level note, or "" if absent."""
    if not task_name:
        return ""
    entry = load_task_scratchpads(root).get(task_name)
    if entry is None:
        return ""
    return entry.get("task") or ""


def load_variant_scratchpad(root: Path, task_name: str, variant: str) -> str:
    """Return the variant-specific note, or "" if absent."""
    if not task_name or not variant:
        return ""
    entry = load_task_scratchpads(root).get(task_name)
    if entry is None:
        return ""
    return (entry.get("variants") or {}).get(variant) or ""


def save_task_scratchpad(root: Path, task_name: str, content: str) -> None:
    """Upsert (or clear) the generic task-level note."""
    if not task_name:
        raise ValueError("task_name is required to save a task scratchpad")
    store = load_task_scratchpads(root)
    entry = store.get(task_name, TaskScratchpadEntry())
    if content:
        entry["task"] = content
    else:
        entry.pop("task", None)
    if entry.get("task") or entry.get("variants"):
        store[task_name] = entry
    else:
        store.pop(task_name, None)
    write_task_scratchpads(root, store)


def save_variant_scratchpad(root: Path, task_name: str, variant: str, content: str) -> None:
    """Upsert (or clear) a variant-specific note."""
    if not task_name or not variant:
        raise ValueError("task_name and variant are required to save a variant scratchpad")
    store = load_task_scratchpads(root)
    entry = store.get(task_name, TaskScratchpadEntry())
    variants = entry.get("variants") or {}
    if content:
        variants[variant] = content
    else:
        variants.pop(variant, None)
    if variants:
        entry["variants"] = variants
    else:
        entry.pop("variants", None)
    if entry.get("task") or entry.get("variants"):
        store[task_name] = entry
    else:
        store.pop(task_name, None)
    write_task_scratchpads(root, store)


# ---------------------------------------------------------------------------
# Pure helpers over score files (no I/O)
# ---------------------------------------------------------------------------

_VOLATILE_SCORE_META_KEYS: frozenset[str] = frozenset({"scored_at"})


def _score_payload_equal(a: ScoreEventScore, b: ScoreEventScore) -> bool:
    """True if two ``score`` payloads represent the same scoring decision."""
    a_meta = {
        k: v for k, v in (a.get("metadata") or {}).items() if k not in _VOLATILE_SCORE_META_KEYS
    }
    b_meta = {
        k: v for k, v in (b.get("metadata") or {}).items() if k not in _VOLATILE_SCORE_META_KEYS
    }
    if a_meta != b_meta:
        return False
    return all(a.get(key) == b.get(key) for key in ("value", "answer", "explanation", "history"))


def stash_matches_submission(stash: SavedScore | None, entry: SavedScore) -> bool:
    """True if the stash's score payload equals the submission's."""
    if stash is None:
        return False
    return _score_payload_equal(stash.get("score") or {}, entry.get("score") or {})


def _entry_author(entry: SavedScore) -> str:
    """Return who wrote this entry — full email, or empty if unknown."""
    meta = (entry.get("score") or {}).get("metadata") or {}
    author = meta.get("author")
    return author if isinstance(author, str) else ""


def _author_matches(author: str, scorer: str) -> bool:
    """True when ``author`` (``score.metadata.author``) equals ``scorer``."""
    return bool(author) and author == scorer


def most_invested_entry_for_scorer(score_file: ScoreFile, scorer: str) -> SavedScore | None:
    """History entry by ``scorer`` with the highest ``minutes_spent_scoring``."""
    own = [e for e in score_file["history"] if _author_matches(_entry_author(e), scorer)]
    if not own:
        return None

    def key(e: SavedScore) -> tuple[float, str]:
        meta = (e.get("score") or {}).get("metadata") or {}
        raw = meta.get("minutes_spent_scoring")
        minutes = float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0.0
        return minutes, e.get("saved_at", "")

    return max(own, key=key)


def has_history_for_scorer(score_file: ScoreFile | None, scorer: str) -> bool:
    if score_file is None:
        return False
    return any(_author_matches(_entry_author(e), scorer) for e in score_file["history"])


def history_for_scorer(score_file: ScoreFile | None, scorer: str) -> list[SavedScore]:
    """All history entries attributed to ``scorer``, in file order."""
    if score_file is None:
        return []
    return [e for e in score_file["history"] if _author_matches(_entry_author(e), scorer)]


def entries_equal_ignoring_saved_at(a: SavedScore, b: SavedScore) -> bool:
    """Two scoring entries match when their score payloads are equivalent."""
    return _score_payload_equal(a.get("score") or {}, b.get("score") or {})


def queue_summary_fields(score_file: ScoreFile | None) -> dict[str, Any]:
    """Latest history entry's user-visible scoring fields for the queue/summary."""
    if score_file is None:
        return {}
    out: dict[str, Any] = {}
    if score_file.get("stash") is not None:
        out["has_stash"] = True
    if not score_file["history"]:
        return out
    latest = max(score_file["history"], key=lambda e: e.get("saved_at", ""))
    score = latest.get("score") or {}
    meta = score.get("metadata") or {}
    if "value" in score:
        out["score"] = score["value"]
    if "explanation" in score:
        out["explanation"] = score["explanation"]
    if "minutes_spent_scoring" in meta:
        out["minutes_to_score"] = meta["minutes_spent_scoring"]
    return out


def derive_status_from_score_file(score_file: ScoreFile | None, fallback: Status) -> Status:
    """Pick the tracking status implied by a score file's current contents.

    Local semantics: there is no "pending push to Hawk" concept, so a sample is
    ``done`` once the user clicks Done Scoring, ``scored`` once any history entry
    exists, else the caller's ``fallback`` (typically ``open``). The
    ``pending_score`` / ``pending_done`` statuses are never produced here.
    """
    if score_file is None:
        return fallback
    if has_finish_event(score_file):
        return STATUS_DONE
    if score_file.get("history"):
        return STATUS_SCORED
    return fallback


def has_finish_event(score_file: ScoreFile | None) -> bool:
    """True if the sample has at least one Done Scoring record."""
    if score_file is None:
        return False
    return bool(score_file.get("finish_events"))
