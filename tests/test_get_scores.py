"""Assert GET /api/scores returns the user's prior scores for a sample.

Same data the panel renders at page-load, exposed as JSON so the UI can
refresh on demand. The route reads the local JSONL store and filters
``score.metadata.author`` against the scorer's full email.
"""

from __future__ import annotations

from flask.testing import FlaskClient

from tests.conftest import SCORER_EMAIL


def _submit(client: FlaskClient, value: float, minutes: int, explanation: str) -> None:
    resp = client.post(
        "/api/submit-score",
        json={
            "value": value,
            "explanation": explanation,
            "minutes_to_score": minutes,
        },
    )
    assert resp.status_code == 200


def test_get_scores_lists_user_submissions(client: FlaskClient) -> None:
    _submit(client, 0.5, 10, "first attempt")
    _submit(client, 0.7, 20, "second attempt — more thought")

    resp = client.get("/api/scores")
    data = resp.get_json()
    assert data["success"] is True
    assert data["scorer_email"] == SCORER_EMAIL
    assert len(data["entries"]) == 2

    # Newest order isn't guaranteed by the route (it just returns file
    # order); panel sorts client-side. Assert by content instead.
    values = sorted(e["score"]["value"] for e in data["entries"])
    assert values == [0.5, 0.7]

    # Author filtering is this route's job; the ScoreEvent/flat dual shape is
    # already pinned in test_submit_score, so don't re-assert it here.
    for e in data["entries"]:
        assert e["score"]["metadata"]["author"] == SCORER_EMAIL


def test_get_scores_includes_finish_events(client: FlaskClient) -> None:
    _submit(client, 0.6, 8, "scored")
    fin = client.post("/api/finish")
    assert fin.status_code == 200

    resp = client.get("/api/scores")
    data = resp.get_json()
    assert len(data["finish_events"]) == 1
    assert data["finish_events"][0]["author"] == SCORER_EMAIL


def test_get_scores_accepts_uuid_param(client: FlaskClient) -> None:
    """Passing ?uuid= targets a different sample (returns empty if absent)."""
    resp = client.get("/api/scores?uuid=zzzzzzzzzzzzzzzzzzzzzz")
    data = resp.get_json()
    assert data["uuid"] == "zzzzzzzzzzzzzzzzzzzzzz"
    assert data["entries"] == []
