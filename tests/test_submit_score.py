"""Assert /api/submit-score persists a score to the local JSONL store.

There is no Hawk in this fork. A successful submit must:

- build the ScoreEvent-shaped entry with the constant ``"manual_score"`` as
  ``scorer`` (the human's identity lives in ``score.metadata.author``), the
  form's value/explanation at the top of ``score``, and
  ``minutes_spent_scoring`` + ``scoring_version`` + ``scored_at`` in
  ``score.metadata``;
- append that entry to the sample's ``history`` (in ``ui_state.jsonl``); and
- append one flat line to ``scores.jsonl`` at the samples root.

The response carries ``hawkless: True`` and an empty ``request_uuid`` for
client-side compatibility. Input validation (the 400 surface and the
all-optional stash path) lives in test_score_validation.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flask.testing import FlaskClient

from scoring.store import load_score_file, scores_path
from tests.conftest import SAMPLE_TASK, SAMPLE_UUID, SCORER_EMAIL


def _basic_form() -> dict[str, Any]:
    return {"value": 0.7, "explanation": "looks good", "minutes_to_score": 12}


def test_submit_score_appends_history_entry(
    client: FlaskClient, samples_dir: Path
) -> None:
    resp = client.post("/api/submit-score", json=_basic_form())
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["hawkless"] is True

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


def test_submit_score_appends_flat_line_to_scores_jsonl(
    client: FlaskClient, samples_dir: Path
) -> None:
    resp = client.post("/api/submit-score", json=_basic_form())
    assert resp.status_code == 200
    data = resp.get_json()

    flat_path = scores_path(samples_dir)
    assert flat_path.exists()
    # The response points the client at this same file.
    assert data["score_path"] == str(flat_path)

    lines = [l for l in flat_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["id"] == SAMPLE_UUID
    assert row["scorer"] == SCORER_EMAIL
    assert row["value"] == 0.7
    assert row["explanation"] == "looks good"
    assert row["minutes_to_score"] == 12
    assert isinstance(row["scored_at"], str)
    assert row["task_name"] == SAMPLE_TASK


def test_submit_score_includes_saved_entry_in_response(client: FlaskClient) -> None:
    resp = client.post("/api/submit-score", json=_basic_form())
    data = resp.get_json()
    saved = data["saved_entry"]
    # Both the ScoreEvent shape and the spread flat form view should be there
    # so the JS prior-scores panel + dedup can each read what they need.
    assert saved["scorer"] == "manual_score"
    assert saved["score"]["value"] == 0.7
    assert saved["value"] == 0.7
    # No Hawk push happens; the response advertises the hawkless path.
    assert data["hawkless"] is True
    assert data["request_uuid"] == ""


def test_submit_score_rejects_duplicate_resubmit(client: FlaskClient) -> None:
    """A no-op resubmit (every score field identical) must not grow the file."""
    first = client.post("/api/submit-score", json=_basic_form())
    assert first.status_code == 200
    dup = client.post("/api/submit-score", json=_basic_form())
    assert dup.status_code == 400
    assert dup.get_json()["success"] is False
