"""Local on-disk store for the manual scoring tool.

Two JSONL files live at the samples root:

- ``history.jsonl`` — one line per sample, keyed by sample ``id`` (the sample
  dir name). A record holds the sample's working/UI state (``history``,
  ``stash``, ``finished_at``, ``scratchpad``, ``timers``) plus an optional
  ``skipped`` flag. Everything else about a sample's status is *derived* from
  this content (see :func:`derive_queue_status`), not stored — a sample the user
  hasn't touched has no record at all. Updates are read-modify-rewrite under a
  module lock.

- ``scores.jsonl`` — canonical flat export, generated on demand (overwritten)
  from ``history.jsonl`` history by ``local-manual-score export`` (see
  :mod:`scoring.export`). One flat line per finalized score (``id``, ``scorer``,
  ``value``, ``explanation``, ``minutes_to_score``, ``scored_at``,
  ``task_name``). It is never read by the tool and is not written on submit.

Status taxonomy (a derived queue-entry value — only ``skipped`` is persisted):

- ``open`` — sample has no saved scoring content yet.
- ``scored`` — at least one history entry exists.
- ``done`` — user clicked "Done Scoring This Sample" (``finished_at`` is set).
- ``skipped`` — user actively skipped; the lone persisted flag, surfaced only
  while the sample has no history/finish of its own.
"""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict, cast

logger = logging.getLogger(__name__)

HISTORY_FILENAME = "history.jsonl"
LEGACY_HISTORY_FILENAME = "ui_state.jsonl"
SCORES_FILENAME = "scores.jsonl"

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


class TimerSnapshot(TypedDict, total=False):
    """Last known state of one stopwatch timer."""

    elapsed_seconds: float


class TimerState(TypedDict, total=False):
    """Per-sample UI-only timer state."""

    normal: TimerSnapshot


class ScoreFile(TypedDict):
    """The working/UI subset of a ``history.jsonl`` record.

    Named ``ScoreFile`` for continuity with the call sites that consume it:
    - ``stash`` (optional): a single in-progress draft.
    - ``history``: append-only list of finalized scoring events.
    - ``finished_at`` (optional): ISO timestamp the user clicked "Done Scoring".
    - ``scratchpad`` (optional): free-form notes for the sample.
    - ``timers`` (optional): persisted stopwatch state.
    """

    stash: NotRequired[SavedScore]
    history: list[SavedScore]
    finished_at: NotRequired[str]
    scratchpad: NotRequired[str]
    timers: NotRequired[TimerState]


# Keys of a record that belong to the ScoreFile (working/UI) view.
_SCORE_KEYS: frozenset[str] = frozenset({"history", "stash", "finished_at", "scratchpad", "timers"})

# Every key persisted in a record besides ``id``: the working/UI score view plus
# the lone ``skipped`` tracking flag. Anything else (e.g. legacy
# status/updated_at/stashed from older formats) is dropped the first
# time a record is rewritten.
_RECORD_KEYS: frozenset[str] = _SCORE_KEYS | {"skipped"}


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Record I/O (history.jsonl)
# ---------------------------------------------------------------------------

# Serializes the read-modify-rewrite on history.jsonl so concurrent writers
# (the background setup thread, timer-state autosaves, the submit handler)
# can't clobber each other.
_records_lock = threading.RLock()


def history_path(root: Path) -> Path:
    return root / HISTORY_FILENAME


def migrate_legacy_history_file(root: Path) -> None:
    """Rename a legacy ``ui_state.jsonl`` to the current ``history.jsonl``.

    The per-sample working/UI state file was once named ``ui_state.jsonl``; it is
    now ``history.jsonl``. This in-place rename keeps existing local data from
    being orphaned by the rename.

    Idempotent and safe to call when neither, either, or both files exist: it
    acts only when the new file is absent *and* the legacy file is present, so a
    second call (or a fresh install) is a no-op. Must be called once at a
    single-threaded startup point, before any threads or Flask start — never
    from inside the records lock, where a rename could race concurrent writes.
    """
    new_path = history_path(root)
    legacy_path = root / LEGACY_HISTORY_FILENAME
    if new_path.exists() or not legacy_path.exists():
        return
    legacy_path.rename(new_path)
    logger.info("Migrated legacy %s -> %s", legacy_path.name, new_path.name)


def scores_path(root: Path) -> Path:
    return root / SCORES_FILENAME


def _load_records(root: Path) -> dict[str, dict[str, Any]]:
    """Read ``history.jsonl`` into ``{id: record}``; ``{}`` if missing.

    Malformed lines are skipped (with a warning) rather than aborting the load,
    so a single bad hand-edit can't take down the scoring page.
    """
    path = history_path(root)
    if not path.exists():
        return {}
    records: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()
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
    """Rewrite ``history.jsonl`` from ``{id: record}``, one line per sample.

    Only ``id`` plus recognized keys (:data:`_RECORD_KEYS`) are written, so
    legacy tracking fields from older formats are dropped on the next rewrite. A
    record left with nothing but ``id`` is omitted entirely.
    """
    path = history_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for sample_id in sorted(records):
            src = records[sample_id]
            rec: dict[str, Any] = {"id": sample_id}
            for key in _RECORD_KEYS:
                if key in src:
                    rec[key] = src[key]
            if len(rec) == 1:  # only ``id`` — nothing worth persisting
                continue
            f.write(json.dumps(rec, sort_keys=True))
            f.write("\n")


def iter_score_files(root: Path) -> list[tuple[str, ScoreFile]]:
    """Return ``(sample_id, ScoreFile)`` for every record with score/UI content.

    A public, read-only view over ``history.jsonl`` for callers (e.g. the
    ``scores.jsonl`` export) that need to walk every sample's working state.
    Skip-only records (no history/stash/etc.) are omitted, since they carry no
    score content. Ordering follows :func:`_load_records` (file order).
    """
    out: list[tuple[str, ScoreFile]] = []
    for sample_id, rec in _load_records(root).items():
        if not any(k in rec for k in _SCORE_KEYS):
            continue
        raw: dict[str, Any] = {k: rec[k] for k in _SCORE_KEYS if k in rec}
        if not isinstance(raw.get("history"), list):
            raw["history"] = []
        out.append((sample_id, cast(ScoreFile, raw)))
    return out


def _ensure_record(records: dict[str, dict[str, Any]], sample_id: str) -> dict[str, Any]:
    """Return the record for ``sample_id``, seeding an empty one if absent."""
    rec = records.get(sample_id)
    if rec is None:
        rec = {"id": sample_id}
        records[sample_id] = rec
    return rec


# ---------------------------------------------------------------------------
# Skip flag (the one persisted tracking bit)
# ---------------------------------------------------------------------------


def load_skipped(root: Path) -> set[str]:
    """Return the set of sample ids the user has skipped."""
    return {sample_id for sample_id, rec in _load_records(root).items() if rec.get("skipped")}


def set_skipped(root: Path, uuid: str, skipped: bool = True) -> None:
    """Set (or clear) the persisted ``skipped`` flag on a sample's record."""
    with _records_lock:
        records = _load_records(root)
        rec = _ensure_record(records, uuid)
        if skipped:
            rec["skipped"] = True
        else:
            rec.pop("skipped", None)
        _write_records(root, records)


def last_activity_map(root: Path) -> dict[str, str]:
    """Map sample id -> latest activity ISO timestamp across its saved content.

    The newest of any history ``saved_at``, the stash ``saved_at``, and the
    ``finished_at`` timestamp; empty string for a record with no timestamped
    content. Powers the summary page's "Updated" column without a persisted field.
    """
    out: dict[str, str] = {}
    for sample_id, rec in _load_records(root).items():
        stamps: list[str] = [
            e["saved_at"]
            for e in (rec.get("history") or [])
            if isinstance(e, dict) and isinstance(e.get("saved_at"), str)
        ]
        stash = rec.get("stash")
        if isinstance(stash, dict) and isinstance(stash.get("saved_at"), str):
            stamps.append(stash["saved_at"])
        finished_at = rec.get("finished_at")
        if isinstance(finished_at, str):
            stamps.append(finished_at)
        out[sample_id] = max(stamps) if stamps else ""
    return out


# ---------------------------------------------------------------------------
# Score-file (working/UI) view
# ---------------------------------------------------------------------------


def load_score_file(root: Path, uuid: str) -> ScoreFile | None:
    """Read the working/UI state for ``uuid``.

    Returns ``None`` when the sample has no record, or a record with no score/UI
    content (e.g. a skip-only record).
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

    The ``skipped`` flag is preserved; score keys absent from ``score_file`` are
    removed (so e.g. clearing the stash drops it).
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


def append_history(root: Path, uuid: str, entry: SavedScore, *, drop_stash: bool = False) -> None:
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


def set_finished(root: Path, uuid: str) -> None:
    """Mark the sample "Done Scoring" by stamping ``finished_at`` on its record."""
    score_file: ScoreFile = load_score_file(root, uuid) or {"history": []}
    score_file["finished_at"] = now_iso()
    write_score_file(root, uuid, score_file)


def clear_finished(root: Path, uuid: str) -> None:
    """Drop the ``finished_at`` stamp so the sample can be re-scored."""
    score_file: ScoreFile = load_score_file(root, uuid) or {"history": []}
    score_file.pop("finished_at", None)
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
# Canonical scores export (scores.jsonl) — pure row building
# ---------------------------------------------------------------------------


def build_score_row(uuid: str, entry: SavedScore, task_name: str) -> dict[str, Any]:
    """Build one flat ``scores.jsonl`` row from a history entry (no I/O).

    ``entry`` is a SavedScore from a sample's history; ``task_name`` is joined in
    by the caller (from the sample's ``sample.json``) so this module needs no
    dependency on :mod:`scoring.state`. The row shape and field derivation match
    the legacy live append: ``scorer`` prefers ``score.metadata.author`` over the
    constant ``entry.scorer``, and ``scored_at`` falls back to the current time.
    """
    score = entry.get("score") or {}
    meta = score.get("metadata") or {}
    return {
        "id": uuid,
        "scorer": meta.get("author") or entry.get("scorer") or "",
        "value": score.get("value"),
        "explanation": score.get("explanation", ""),
        "minutes_to_score": meta.get("minutes_spent_scoring"),
        "scored_at": meta.get("scored_at") or now_iso(),
        "task_name": task_name or "",
    }


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
        minutes = float(raw) if isinstance(raw, int | float) and not isinstance(raw, bool) else 0.0
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
    """Status implied by a score file's contents, ignoring the skip flag.

    A sample is ``done`` once the user clicks Done Scoring, ``scored`` once any
    history entry exists, else the caller's ``fallback`` (typically ``open``).
    The skip-aware wrapper is :func:`derive_queue_status`.
    """
    if score_file is None:
        return fallback
    if has_finish_event(score_file):
        return STATUS_DONE
    if score_file.get("history"):
        return STATUS_SCORED
    return fallback


def has_finish_event(score_file: ScoreFile | None) -> bool:
    """True if the sample has been marked Done Scoring (``finished_at`` set)."""
    return bool(score_file and score_file.get("finished_at"))


def derive_queue_status(
    score_file: ScoreFile | None,
    *,
    skipped: bool,
    include_skipped: bool = False,
    include_done: bool = False,
) -> Status:
    """The queue-entry status for a sample, from its score file + skip flag.

    Precedence ``done`` > ``scored`` > ``skipped`` > ``open``. ``skipped`` is the
    one persisted bit, and it surfaces only while the sample has no history or
    finish of its own — skipping is a UI action on an otherwise-untouched sample,
    so the flag never needs clearing once a real score lands. ``include_skipped``
    / ``include_done`` re-surface those samples as navigable (``open`` /
    ``scored``).
    """
    base = derive_status_from_score_file(score_file, STATUS_OPEN)
    if base == STATUS_DONE and include_done:
        base = STATUS_SCORED if (score_file and score_file.get("history")) else STATUS_OPEN
    if base == STATUS_OPEN and skipped and not include_skipped:
        return STATUS_SKIPPED
    return base
