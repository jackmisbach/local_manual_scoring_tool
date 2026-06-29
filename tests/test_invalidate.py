"""Tests for score invalidation: the ``/api/invalidate`` route, the persisted
``invalidations`` list, and the ``/api/scores`` surface (``invalidated`` set +
per-entry ``local_event_uuid``).

All I/O is against a tmp dir via the Flask test client — no Docker, network, or
Hawk.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from flask.testing import FlaskClient

from scoring.store import SavedScore, load_score_file, local_event_uuid_of
from tests.conftest import SAMPLE_UUID, SCORER_EMAIL


def _entry(value: float = 0.5) -> SavedScore:
    return {
        "scorer": "manual_score",
        "score": {
            "value": value,
            "answer": None,
            "explanation": "because",
            "history": [],
            "metadata": {
                "author": SCORER_EMAIL,
                "minutes_spent_scoring": 5.0,
                "scored_at": "2026-06-25T00:00:00Z",
            },
        },
    }


def test_invalidate_persists_and_is_idempotent(client: FlaskClient, samples_dir: Path) -> None:
    target = "evt-xyz"
    resp = client.post(
        "/api/invalidate", json={"local_event_uuid": target, "reason": "wrong value"}
    )
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True

    # Idempotent: a second invalidation of the same target adds no duplicate.
    client.post("/api/invalidate", json={"local_event_uuid": target})

    score_file = load_score_file(samples_dir, SAMPLE_UUID)
    assert score_file is not None
    invs = score_file["invalidations"]
    assert len(invs) == 1
    assert invs[0]["local_target_event_uuid"] == target
    assert invs[0]["author"] == SCORER_EMAIL
    assert invs[0]["reason"] == "wrong value"
    assert invs[0].get("invalidated_at")


def test_invalidate_missing_uuid_is_400(client: FlaskClient) -> None:
    assert client.post("/api/invalidate", json={}).status_code == 400
    assert client.post("/api/invalidate", json={"local_event_uuid": ""}).status_code == 400
    assert client.post("/api/invalidate", json={"local_event_uuid": 5}).status_code == 400


def test_scores_returns_invalidated_and_entry_uuids(client: FlaskClient, samples_dir: Path) -> None:
    # Submit a real score so the sample has a history entry with a uuid.
    submit = client.post(
        "/api/submit-score",
        json={"value": 0.7, "explanation": "looks good", "minutes_to_score": 12},
    )
    assert submit.status_code == 200
    target = submit.get_json()["saved_entry"]["local_event_uuid"]

    client.post("/api/invalidate", json={"local_event_uuid": target, "reason": "redo"})

    scores = client.get("/api/scores").get_json()
    assert scores["invalidated"] == [target]
    assert len(scores["entries"]) == 1
    assert scores["entries"][0]["local_event_uuid"] == target


def test_local_event_uuid_of_stored_else_raises() -> None:
    """The store helper returns the stored uuid when present, else raises."""
    stored = _entry()
    stored["score"]["metadata"]["local_event_uuid"] = "explicit"
    assert local_event_uuid_of(stored) == "explicit"

    with pytest.raises(ValueError, match="local_event_uuid"):
        local_event_uuid_of(_entry())
