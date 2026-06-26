"""Assert /api/finish records a local "done scoring" event for the sample.

There is no Hawk in this fork: ``/api/finish`` appends a ``finish_events`` row
to the sample's record in ``ui_state.jsonl`` (via
:func:`scoring.store.append_finish_event`) and the resulting derived status is
``done`` so navigation stops landing on the sample.
"""

from __future__ import annotations

from pathlib import Path

from flask.testing import FlaskClient

from scoring.store import (
    STATUS_DONE,
    has_finish_event,
    load_score_file,
    load_tracking,
)
from tests.conftest import SAMPLE_UUID, SCORER_EMAIL


def test_finish_returns_success(client: FlaskClient) -> None:
    resp = client.post("/api/finish")
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True


def test_finish_records_finish_event(client: FlaskClient, samples_dir: Path) -> None:
    resp = client.post("/api/finish")
    assert resp.status_code == 200

    score_file = load_score_file(samples_dir, SAMPLE_UUID)
    assert score_file is not None
    assert has_finish_event(score_file) is True

    events = score_file.get("finish_events") or []
    assert len(events) == 1
    record = events[0]
    assert record["author"] == SCORER_EMAIL
    assert isinstance(record.get("sent_at"), str)


def test_finish_marks_sample_done(client: FlaskClient, samples_dir: Path) -> None:
    resp = client.post("/api/finish")
    assert resp.status_code == 200

    # The tracking row (persisted to ui_state.jsonl by save_state) reads done.
    tracking = load_tracking(samples_dir)
    assert tracking[SAMPLE_UUID]["status"] == STATUS_DONE
