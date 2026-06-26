"""Cover the consolidated /api/open and /api/toggle routes.

``/api/open`` selects what to open via a ``target`` discriminator
(``path`` / ``artifacts`` / ``config``); ``/api/toggle`` flips a session
boolean via a ``setting`` discriminator (``autostart`` / ``attach``).

``open_in_os_default`` is monkeypatched in every test that exercises ``/api/open``
so nothing actually launches a GUI app — we record the path it was asked to open
instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from flask.testing import FlaskClient

from scoring import app as app_module


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture the paths ``open_in_os_default`` is asked to open (no real launch)."""
    paths: list[str] = []
    monkeypatch.setattr(app_module, "open_in_os_default", paths.append)
    return paths


def test_open_config_succeeds(client: FlaskClient, opened: list[str]) -> None:
    resp = client.post("/api/open", json={"target": "config"})
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    # The config file path was handed to the OS-default opener.
    assert len(opened) == 1
    assert opened[0].endswith("config.json")


def test_open_artifacts_succeeds(client: FlaskClient, opened: list[str]) -> None:
    resp = client.post("/api/open", json={"target": "artifacts"})
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    assert len(opened) == 1


def test_open_path_inside_samples_dir(
    client: FlaskClient, samples_dir: Path, opened: list[str]
) -> None:
    from tests.conftest import SAMPLE_UUID

    target = samples_dir / SAMPLE_UUID / "sample.json"
    resp = client.post("/api/open", json={"target": "path", "path": str(target)})
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    assert opened == [str(target.resolve())]


def test_open_path_outside_samples_dir_rejected(
    client: FlaskClient, tmp_path: Path, opened: list[str]
) -> None:
    outside = tmp_path.parent / "not-a-sample.txt"
    resp = client.post("/api/open", json={"target": "path", "path": str(outside)})
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False
    assert opened == []  # never launched


def test_open_path_missing_path_field(client: FlaskClient, opened: list[str]) -> None:
    resp = client.post("/api/open", json={"target": "path"})
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False
    assert opened == []


def test_open_unknown_target_rejected(client: FlaskClient, opened: list[str]) -> None:
    resp = client.post("/api/open", json={"target": "bogus"})
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False
    assert opened == []


def test_open_missing_target_rejected(client: FlaskClient, opened: list[str]) -> None:
    resp = client.post("/api/open", json={})
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False
    assert opened == []


@pytest.mark.parametrize("setting", ["attach", "autostart"])
def test_toggle_flips_setting(client: FlaskClient, setting: str) -> None:
    first = client.post("/api/toggle", json={"setting": setting})
    assert first.status_code == 200
    data = first.get_json()
    assert data["success"] is True
    assert data["setting"] == setting
    assert isinstance(data["value"], bool)

    # A second toggle returns the opposite value — the flip is real and persisted.
    second = client.post("/api/toggle", json={"setting": setting})
    assert second.get_json()["value"] is not data["value"]


def test_toggle_unknown_setting_rejected(client: FlaskClient) -> None:
    resp = client.post("/api/toggle", json={"setting": "bogus"})
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False
