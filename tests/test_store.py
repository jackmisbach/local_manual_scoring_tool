"""Tests for the local JSONL store (scoring.store).

Covers the two files at the samples root:

- ``ui_state.jsonl``: keyed per-sample records holding both the tracking subset
  (status / updated_at / stashed) and the working/UI subset
  (history / stash / finish_events / scratchpad / timers); and
- ``scores.jsonl``: the append-only flat export of finalized scores.

All I/O is against a tmp dir, so these tests need no Docker, network, or Hawk.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scoring.store import (
    SCORES_FILENAME,
    STATUS_DONE,
    STATUS_OPEN,
    STATUS_SCORED,
    STATUS_SKIPPED,
    UI_STATE_FILENAME,
    SavedScore,
    TrackedSample,
    append_finish_event,
    append_history,
    append_score_jsonl,
    clear_stash,
    derive_status_from_score_file,
    has_finish_event,
    load_score_file,
    load_tracking,
    save_stash,
    save_tracking,
    ui_state_path,
)

UUID = "abcdefghijklmnopqrstuv"
SCORER = "alice@metr.org"


def _entry(value: float = 0.5, minutes: float = 7.0, author: str = SCORER) -> SavedScore:
    return {
        "scorer": "manual_score",
        "score": {
            "value": value,
            "answer": None,
            "explanation": "because",
            "history": [],
            "metadata": {
                "author": author,
                "minutes_spent_scoring": minutes,
                "scored_at": "2026-06-25T00:00:00Z",
            },
        },
    }


# --- tracking round-trip --------------------------------------------------


def test_tracking_round_trips_through_ui_state(tmp_path: Path) -> None:
    tracking: dict[str, TrackedSample] = {
        UUID: {
            "status": STATUS_SCORED,
            "updated_at": "2026-06-25T00:00:00Z",
        }
    }
    save_tracking(tmp_path, tracking, [UUID])

    assert ui_state_path(tmp_path) == tmp_path / UI_STATE_FILENAME
    assert ui_state_path(tmp_path).exists()

    loaded = load_tracking(tmp_path)
    assert loaded[UUID]["status"] == STATUS_SCORED
    assert loaded[UUID]["updated_at"] == "2026-06-25T00:00:00Z"
    # No stash draft -> the stashed flag is absent (treated as False).
    assert "stashed" not in loaded[UUID]


def test_save_tracking_only_rewrites_touched_rows(tmp_path: Path) -> None:
    """A second writer that touches one id leaves the other id's row intact."""
    save_tracking(
        tmp_path,
        {UUID: {"status": STATUS_OPEN, "updated_at": "t1"}},
        [UUID],
    )
    other = "zzzzzzzzzzzzzzzzzzzzzz"
    save_tracking(
        tmp_path,
        {other: {"status": STATUS_SKIPPED, "updated_at": "t2"}},
        [other],
    )
    loaded = load_tracking(tmp_path)
    assert loaded[UUID]["status"] == STATUS_OPEN
    assert loaded[other]["status"] == STATUS_SKIPPED


# --- history --------------------------------------------------------------


def test_append_history_then_load_score_file(tmp_path: Path) -> None:
    append_history(tmp_path, UUID, _entry(value=0.4))
    append_history(tmp_path, UUID, _entry(value=0.9, minutes=20))

    score_file = load_score_file(tmp_path, UUID)
    assert score_file is not None
    assert len(score_file["history"]) == 2
    assert [e["score"]["value"] for e in score_file["history"]] == [0.4, 0.9]
    # append_history stamps saved_at.
    assert all(isinstance(e.get("saved_at"), str) for e in score_file["history"])


def test_load_score_file_missing_returns_none(tmp_path: Path) -> None:
    assert load_score_file(tmp_path, UUID) is None


def test_bare_tracking_row_has_no_score_file(tmp_path: Path) -> None:
    """A sample with only a tracking row (no score keys) reads as no score file."""
    save_tracking(
        tmp_path,
        {UUID: {"status": STATUS_OPEN, "updated_at": "t"}},
        [UUID],
    )
    assert load_score_file(tmp_path, UUID) is None


# --- stash ----------------------------------------------------------------


def test_save_stash_then_clear_stash(tmp_path: Path) -> None:
    append_history(tmp_path, UUID, _entry(value=0.3))
    save_stash(tmp_path, UUID, _entry(value=0.8))

    score_file = load_score_file(tmp_path, UUID)
    assert score_file is not None
    assert score_file.get("stash") is not None
    assert score_file["stash"]["score"]["value"] == 0.8
    # Saving a stash preserves history.
    assert len(score_file["history"]) == 1

    cleared = clear_stash(tmp_path, UUID)
    assert cleared is not None
    assert cleared.get("stash") is None
    # History survives the stash clear.
    assert len(cleared["history"]) == 1
    assert load_score_file(tmp_path, UUID).get("stash") is None  # type: ignore[union-attr]


# --- derive_status_from_score_file ----------------------------------------


def test_derive_status_open_when_no_history(tmp_path: Path) -> None:
    assert derive_status_from_score_file(None, STATUS_OPEN) == STATUS_OPEN
    save_stash(tmp_path, UUID, _entry())
    score_file = load_score_file(tmp_path, UUID)
    # A stash with empty history derives the fallback.
    assert derive_status_from_score_file(score_file, STATUS_OPEN) == STATUS_OPEN


def test_derive_status_scored_when_history_exists(tmp_path: Path) -> None:
    append_history(tmp_path, UUID, _entry())
    score_file = load_score_file(tmp_path, UUID)
    assert derive_status_from_score_file(score_file, STATUS_OPEN) == STATUS_SCORED


def test_derive_status_done_when_finish_event(tmp_path: Path) -> None:
    append_history(tmp_path, UUID, _entry())
    append_finish_event(tmp_path, UUID, SCORER)
    score_file = load_score_file(tmp_path, UUID)
    assert has_finish_event(score_file) is True
    # Finish wins over scored.
    assert derive_status_from_score_file(score_file, STATUS_OPEN) == STATUS_DONE


# --- finish events --------------------------------------------------------


def test_append_finish_event_records_author_and_timestamp(tmp_path: Path) -> None:
    append_finish_event(tmp_path, UUID, SCORER)
    score_file = load_score_file(tmp_path, UUID)
    assert score_file is not None
    events = score_file.get("finish_events") or []
    assert len(events) == 1
    assert events[0]["author"] == SCORER
    assert isinstance(events[0].get("sent_at"), str)


# --- scores.jsonl flat export ---------------------------------------------


def test_append_score_jsonl_writes_flat_parseable_line(tmp_path: Path) -> None:
    entry = _entry(value=0.6, minutes=15)
    info: dict[str, Any] = {"task_name": "demo"}
    path = append_score_jsonl(tmp_path, UUID, entry, info)

    assert path == tmp_path / SCORES_FILENAME
    assert path.exists()
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert set(row) == {
        "id",
        "scorer",
        "value",
        "explanation",
        "minutes_to_score",
        "scored_at",
        "task_name",
    }
    assert row["id"] == UUID
    assert row["scorer"] == SCORER
    assert row["value"] == 0.6
    assert row["explanation"] == "because"
    assert row["minutes_to_score"] == 15
    assert row["scored_at"] == "2026-06-25T00:00:00Z"
    assert row["task_name"] == "demo"


def test_append_score_jsonl_appends(tmp_path: Path) -> None:
    info: dict[str, Any] = {"task_name": "demo"}
    append_score_jsonl(tmp_path, UUID, _entry(value=0.1), info)
    append_score_jsonl(tmp_path, UUID, _entry(value=0.2), info)
    path = tmp_path / SCORES_FILENAME
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 2
    assert [json.loads(l)["value"] for l in lines] == [0.1, 0.2]
