"""Tests for the local JSONL store (scoring.store).

Covers ``history.jsonl`` at the samples root: keyed per-sample records holding
the working/UI subset (history / stash / finished_at / scratchpad / timers /
invalidations) plus the lone persisted ``skipped`` flag. The per-sample
score-log export builders (:func:`scoring.store.build_score_event` etc.) are
round-tripped through ``test_export.py``; the local-event-uuid + invalidation
helpers are exercised here and in ``test_invalidate.py``.

All I/O is against a tmp dir, so these tests need no Docker, network, or Hawk.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from scoring.store import (
    HISTORY_FILENAME,
    STATUS_DONE,
    STATUS_OPEN,
    STATUS_SCORED,
    STATUS_SKIPPED,
    SavedScore,
    add_invalidation,
    append_history,
    build_invalidation_event,
    build_score_event,
    clear_finished,
    clear_stash,
    derive_queue_status,
    derive_status_from_score_file,
    has_finish_event,
    history_path,
    invalidated_local_uuids,
    iter_score_files,
    load_score_file,
    load_skipped,
    local_event_uuid_of,
    save_stash,
    set_finished,
    set_skipped,
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


# --- skip flag ------------------------------------------------------------


def test_set_and_load_skipped(tmp_path: Path) -> None:
    assert load_skipped(tmp_path) == set()
    set_skipped(tmp_path, UUID)

    assert history_path(tmp_path) == tmp_path / HISTORY_FILENAME
    assert load_skipped(tmp_path) == {UUID}

    # Clearing the flag leaves a bare record, which is dropped on rewrite.
    set_skipped(tmp_path, UUID, False)
    assert load_skipped(tmp_path) == set()
    assert not history_path(tmp_path).read_text(encoding="utf-8").strip()


def test_skip_flag_survives_score_writes(tmp_path: Path) -> None:
    """A real score landing on a skipped sample preserves the skip flag."""
    set_skipped(tmp_path, UUID)
    append_history(tmp_path, UUID, _entry())
    assert load_skipped(tmp_path) == {UUID}
    score_file = load_score_file(tmp_path, UUID)
    assert score_file is not None
    assert len(score_file["history"]) == 1


def test_write_drops_legacy_tracking_keys(tmp_path: Path) -> None:
    """A record with only legacy tracking keys is dropped on the next rewrite."""
    history_path(tmp_path).write_text(
        json.dumps({"id": UUID, "status": "open", "updated_at": "t", "stashed": True}) + "\n",
        encoding="utf-8",
    )
    # Any write rewrites the whole file; the legacy-only row has no recognized
    # content and is omitted, while the touched sample is written.
    other = "zzzzzzzzzzzzzzzzzzzzzz"
    append_history(tmp_path, other, _entry())
    ids = {
        json.loads(line)["id"]
        for line in history_path(tmp_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    assert ids == {other}


# --- derive_queue_status --------------------------------------------------


def test_derive_queue_status_skip_precedence(tmp_path: Path) -> None:
    # Untouched + skip flag -> skipped; include_skipped re-surfaces as open.
    assert derive_queue_status(None, skipped=False) == STATUS_OPEN
    assert derive_queue_status(None, skipped=True) == STATUS_SKIPPED
    assert derive_queue_status(None, skipped=True, include_skipped=True) == STATUS_OPEN

    # A real score outranks the skip flag (no need to ever clear it).
    append_history(tmp_path, UUID, _entry())
    scored = load_score_file(tmp_path, UUID)
    assert derive_queue_status(scored, skipped=True) == STATUS_SCORED

    # Finish wins; include_done demotes done back to scored.
    set_finished(tmp_path, UUID)
    done = load_score_file(tmp_path, UUID)
    assert derive_queue_status(done, skipped=True) == STATUS_DONE
    assert derive_queue_status(done, skipped=True, include_done=True) == STATUS_SCORED


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


def test_skip_only_record_has_no_score_file(tmp_path: Path) -> None:
    """A sample with only a skip flag (no score keys) reads as no score file."""
    set_skipped(tmp_path, UUID)
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


def test_derive_status_done_when_finished(tmp_path: Path) -> None:
    append_history(tmp_path, UUID, _entry())
    set_finished(tmp_path, UUID)
    score_file = load_score_file(tmp_path, UUID)
    assert has_finish_event(score_file) is True
    # Finish wins over scored.
    assert derive_status_from_score_file(score_file, STATUS_OPEN) == STATUS_DONE


# --- finished flag --------------------------------------------------------


def test_set_finished_records_timestamp(tmp_path: Path) -> None:
    set_finished(tmp_path, UUID)
    score_file = load_score_file(tmp_path, UUID)
    assert score_file is not None
    assert has_finish_event(score_file) is True
    finished_at = score_file.get("finished_at")
    assert isinstance(finished_at, str)
    assert finished_at


def test_clear_finished_drops_timestamp(tmp_path: Path) -> None:
    set_finished(tmp_path, UUID)
    clear_finished(tmp_path, UUID)
    score_file = load_score_file(tmp_path, UUID)
    # The bare record (only the dropped flag) is omitted entirely on rewrite.
    assert has_finish_event(score_file) is False


# --- local_event_uuid_of --------------------------------------------------


def test_local_event_uuid_of_returns_stored() -> None:
    entry = _entry(value=0.6)
    entry["score"]["metadata"]["local_event_uuid"] = "stored-uuid"
    assert local_event_uuid_of(entry, UUID) == "stored-uuid"


def test_local_event_uuid_of_derives_stably_when_absent() -> None:
    """A pre-feature entry (no stored uuid) derives a stable, valid uuid5."""
    entry = _entry(value=0.6)
    first = local_event_uuid_of(entry, UUID)
    second = local_event_uuid_of(entry, UUID)
    assert first == second  # deterministic
    # A valid uuid string.
    uuid.UUID(first)
    # Empty stored value also falls through to the derivation.
    entry["score"]["metadata"]["local_event_uuid"] = ""
    assert local_event_uuid_of(entry, UUID) == first
    # A different sample id yields a different derived uuid.
    assert local_event_uuid_of(entry, "other-sample") != first


# --- invalidations --------------------------------------------------------


def test_add_invalidation_is_idempotent(tmp_path: Path) -> None:
    add_invalidation(tmp_path, UUID, "evt-1", SCORER, "wrong")
    add_invalidation(tmp_path, UUID, "evt-1", SCORER, "wrong again")
    add_invalidation(tmp_path, UUID, "evt-2", SCORER, "")
    score_file = load_score_file(tmp_path, UUID)
    assert score_file is not None
    invs = score_file["invalidations"]
    assert [i["local_target_event_uuid"] for i in invs] == ["evt-1", "evt-2"]
    # The first (winning) record keeps its original reason; each is stamped.
    assert invs[0]["reason"] == "wrong"
    assert all(i.get("invalidated_at") for i in invs)
    assert invalidated_local_uuids(score_file) == {"evt-1", "evt-2"}


def test_invalidated_local_uuids_none() -> None:
    assert invalidated_local_uuids(None) == set()


# --- score-log builders ---------------------------------------------------


def test_build_score_event_shape() -> None:
    entry = _entry(value=0.6, minutes=15)
    event = build_score_event(entry, UUID)
    assert event["event_type"] == "score"
    assert event["scorer"] == "manual_score"
    meta = event["score"]["metadata"]
    # Preserves existing metadata and ensures local_event_uuid is set.
    assert meta["author"] == SCORER
    assert meta["minutes_spent_scoring"] == 15
    assert meta["local_event_uuid"] == local_event_uuid_of(entry, UUID)


def test_build_invalidation_event_shape() -> None:
    event = build_invalidation_event(
        {"local_target_event_uuid": "evt-1", "author": SCORER, "reason": "wrong"}
    )
    assert event["event_type"] == "info"
    assert event["info_source"] == "event_invalidation"
    assert event["info_data"]["target_event_uuid"] is None
    assert event["info_data"]["local_target_event_uuid"] == "evt-1"
    prov = event["info_data"]["provenance"]
    assert prov["author"] == SCORER
    assert prov["reason"] == "wrong"
    assert prov["metadata"]["tool"] == "local-manual-scoring-tool"


# --- iter_score_files -----------------------------------------------------


def test_iter_score_files_skips_skip_only_records(tmp_path: Path) -> None:
    other = "zzzzzzzzzzzzzzzzzzzzzz"
    append_history(tmp_path, UUID, _entry(value=0.4))
    set_skipped(tmp_path, other)  # skip-only record has no score content

    by_id = dict(iter_score_files(tmp_path))
    assert set(by_id) == {UUID}
    assert [e["score"]["value"] for e in by_id[UUID]["history"]] == [0.4]
