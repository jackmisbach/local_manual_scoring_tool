"""Assert /api/submit-score persists a score to the local JSONL store.

There is no Hawk in this fork. A successful submit must:

- build the ScoreEvent-shaped entry with the constant ``"manual_score"`` as
  ``scorer`` (the human's identity lives in ``score.metadata.author``), the
  form's value/explanation at the top of ``score``, and
  ``minutes_spent_scoring`` + ``scoring_version`` + ``scored_at`` in
  ``score.metadata``; and
- append that entry to the sample's ``history`` (in ``history.jsonl``).

It must NOT write the score-log export — that's generated on demand by
``local-manual-score export`` (round-tripped in test_export.py).

The response carries the local ``score_path`` (where the score persisted —
``history.jsonl``). Input validation (the 400 surface and the all-optional
stash path) lives in test_score_validation.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask.testing import FlaskClient

from scoring.store import history_path, load_score_file
from tests.conftest import SAMPLE_UUID, SCORER_EMAIL


def _basic_form() -> dict[str, Any]:
    return {"value": 0.7, "explanation": "looks good", "minutes_to_score": 12}


def test_submit_score_appends_history_entry(client: FlaskClient, samples_dir: Path) -> None:
    resp = client.post("/api/submit-score", json=_basic_form())
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True

    local = load_score_file(samples_dir, SAMPLE_UUID)
    assert local is not None
    assert len(local["history"]) == 1
    entry = local["history"][-1]
    assert entry["scorer"] == "manual_score"

    score = entry["score"]
    assert score["value"] == 0.7
    assert score["explanation"] == "looks good"
    assert score["answer"] is None
    assert score["history"] == []

    meta = score["metadata"]
    assert meta["author"] == SCORER_EMAIL
    assert meta["minutes_spent_scoring"] == 12
    assert isinstance(meta.get("scoring_version"), str)
    # scored_at is decision-time provenance kept in the local audit trail.
    assert isinstance(meta.get("scored_at"), str)
    # Every finalized score is tagged with a fresh local event uuid.
    assert isinstance(meta.get("local_event_uuid"), str)
    assert meta["local_event_uuid"]


def test_submit_score_does_not_write_export(client: FlaskClient, samples_dir: Path) -> None:
    """The score-log export is generated on demand — submit must not write it."""
    resp = client.post("/api/submit-score", json=_basic_form())
    assert resp.status_code == 200
    assert not (samples_dir / "output_scores.zip").exists()
    assert not (samples_dir / "scores.jsonl").exists()


def test_submit_score_response_points_at_history(client: FlaskClient, samples_dir: Path) -> None:
    """The response ``score_path`` is where the score actually persists."""
    resp = client.post("/api/submit-score", json=_basic_form())
    data = resp.get_json()
    assert data["score_path"] == str(history_path(samples_dir))
    assert history_path(samples_dir).exists()


def test_submit_score_includes_saved_entry_in_response(client: FlaskClient) -> None:
    resp = client.post("/api/submit-score", json=_basic_form())
    data = resp.get_json()
    saved = data["saved_entry"]
    # Both the ScoreEvent shape and the spread flat form view should be there
    # so the JS prior-scores panel + dedup can each read what they need.
    assert saved["scorer"] == "manual_score"
    assert saved["score"]["value"] == 0.7
    assert saved["value"] == 0.7
    # The resolved local event uuid is exposed for the client to invalidate.
    assert saved["local_event_uuid"] == saved["score"]["metadata"]["local_event_uuid"]


def test_submit_score_rejects_duplicate_resubmit(client: FlaskClient) -> None:
    """A no-op resubmit (every score field identical) must not grow the file."""
    first = client.post("/api/submit-score", json=_basic_form())
    assert first.status_code == 200
    dup = client.post("/api/submit-score", json=_basic_form())
    assert dup.status_code == 400
    assert dup.get_json()["success"] is False
