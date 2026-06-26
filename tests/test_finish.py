"""Assert /api/finish marks the sample "done scoring".

There is no Hawk in this fork: ``/api/finish`` stamps ``finished_at`` on the
sample's record in ``history.jsonl`` (via :func:`scoring.store.set_finished`)
and the resulting derived status is ``done`` so navigation stops landing on the
sample.
"""

from __future__ import annotations

from pathlib import Path

from flask.testing import FlaskClient

from scoring.store import (
    STATUS_DONE,
    derive_queue_status,
    has_finish_event,
    load_score_file,
)
from tests.conftest import SAMPLE_UUID


def test_finish_returns_success(client: FlaskClient) -> None:
    resp = client.post("/api/finish")
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True


def test_finish_records_finished_at(client: FlaskClient, samples_dir: Path) -> None:
    resp = client.post("/api/finish")
    assert resp.status_code == 200

    score_file = load_score_file(samples_dir, SAMPLE_UUID)
    assert score_file is not None
    assert has_finish_event(score_file) is True
    assert isinstance(score_file.get("finished_at"), str)


def test_finish_marks_sample_done(client: FlaskClient, samples_dir: Path) -> None:
    resp = client.post("/api/finish")
    assert resp.status_code == 200

    # The sample's derived status reads done once a finish event is recorded.
    score_file = load_score_file(samples_dir, SAMPLE_UUID)
    assert derive_queue_status(score_file, skipped=False) == STATUS_DONE
