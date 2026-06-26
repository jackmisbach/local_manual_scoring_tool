"""Validation of the flat score-form payload (PR-rewritten _parse_score_payload).

Pins the 400 surface that stops an invalid score from reaching Hawk
(range / finite / parse / missing-field) plus the all-optional stash path,
none of which the happy-path submit tests in test_submit_score.py exercise.
"""

from __future__ import annotations

from typing import Any

import pytest
from flask.testing import FlaskClient


@pytest.mark.parametrize(
    ("body", "error_substr"),
    [
        ({"value": 1.5, "minutes_to_score": 5, "explanation": "x"}, "between 0.0 and 1.0"),
        ({"value": -0.1, "minutes_to_score": 5, "explanation": "x"}, "between 0.0 and 1.0"),
        ({"value": "nan", "minutes_to_score": 5, "explanation": "x"}, "must be finite"),
        ({"value": "abc", "minutes_to_score": 5, "explanation": "x"}, "Invalid value value"),
        (
            {"value": 0.5, "minutes_to_score": -1, "explanation": "x"},
            "minutes_to_score must be >= 0.0",
        ),
        ({"minutes_to_score": 5, "explanation": "x"}, "Missing value"),
        ({"value": 0.5, "explanation": "x"}, "Missing minutes_to_score"),
        ({"value": 0.5, "minutes_to_score": 5}, "Missing explanation"),
    ],
)
def test_submit_score_rejects_invalid_payload(
    client: FlaskClient, body: dict[str, Any], error_substr: str
) -> None:
    resp = client.post("/api/submit-score", json=body)
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["success"] is False
    assert error_substr in data["error"]


def test_submit_score_rejects_non_object_body(client: FlaskClient) -> None:
    resp = client.post("/api/submit-score", json=[1, 2, 3])
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Invalid request"


@pytest.mark.parametrize(
    "body",
    [
        {"value": 0.5},
        {"explanation": "work in progress"},
        {"minutes_to_score": 4},
        {},
    ],
)
def test_stash_accepts_partial_payload(client: FlaskClient, body: dict[str, Any]) -> None:
    resp = client.post("/api/stash", json=body)
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True


def test_stash_still_range_checks(client: FlaskClient) -> None:
    # require_complete is False for a stash, but range/parse checks still run,
    # so a stash can't smuggle an out-of-range value past validation.
    resp = client.post("/api/stash", json={"value": 2.0})
    assert resp.status_code == 400
    assert "between 0.0 and 1.0" in resp.get_json()["error"]
