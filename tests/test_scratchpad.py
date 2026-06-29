"""Per-sample scratch-pad storage and the save route.

The scratch pad is free-form per-sample notes stored inside the sample's
``history.jsonl`` record under ``scratchpad``. It never lands in the score-log
export and has no task/variant promotion — each sample's pad is its own.
"""

from __future__ import annotations

from pathlib import Path

from flask.testing import FlaskClient

from scoring.store import load_scratchpad, save_scratchpad
from tests.conftest import SAMPLE_UUID


def test_missing_scratchpad_loads_empty(artifacts_dir: Path) -> None:
    assert load_scratchpad(artifacts_dir, SAMPLE_UUID) == ""


def test_scratchpad_roundtrip(artifacts_dir: Path) -> None:
    save_scratchpad(artifacts_dir, SAMPLE_UUID, "sample notes")
    assert load_scratchpad(artifacts_dir, SAMPLE_UUID) == "sample notes"


def test_clearing_scratchpad_drops_the_note(artifacts_dir: Path) -> None:
    save_scratchpad(artifacts_dir, SAMPLE_UUID, "sample notes")
    save_scratchpad(artifacts_dir, SAMPLE_UUID, "")
    assert load_scratchpad(artifacts_dir, SAMPLE_UUID) == ""


# --- save route -----------------------------------------------------------


def test_save_scratchpad_route_persists(client: FlaskClient, artifacts_dir: Path) -> None:
    resp = client.post("/api/scratchpad", json={"content": "from the route"})
    assert resp.get_json()["success"] is True
    assert load_scratchpad(artifacts_dir, SAMPLE_UUID) == "from the route"


def test_save_scratchpad_route_requires_content(client: FlaskClient) -> None:
    resp = client.post("/api/scratchpad", json={})
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False
