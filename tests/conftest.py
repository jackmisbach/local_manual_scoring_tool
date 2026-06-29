"""Pytest fixtures for the manual-scoring Flask app (local, Hawk-free fork).

Each test gets:

- ``samples_dir`` / ``artifacts_dir``: a tmp samples root containing one
  sample dir (``<root>/<uuid>/project/`` plus a ``sample.json``). Both names
  point at the same directory — ``artifacts_dir`` is kept so the existing tests
  read naturally; the store and state code both call this the "samples root".
- a Flask ``client`` wired up via :func:`scoring.app.create_app` with a stub
  :class:`~scoring.state.ScoringState` and ``no_container=True`` so no Docker
  is touched.

There is no Hawk in this fork: scores live in ``history.jsonl`` at the samples
root (the score-log ``output_scores.zip`` is a generated export, not written on
submit — see test_export.py), so the routes do real on-disk reads/writes against
the tmp dir and there is nothing to monkeypatch at the network boundary.

Config (scorer email, task selection, task guidance, the change banner) lives
in a per-user config file under ``$XDG_CONFIG_HOME``; an autouse fixture points
that at a tmp dir so a developer's real config can't leak into a test and so
``drain_changes()`` on the index route stays deterministic.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from scoring import app as app_module
from scoring.state import ScoringState, local_sample_info

SAMPLE_UUID = "abcdefghijklmnopqrstuv"  # 22 chars
SCORER_EMAIL = "alice@metr.org"

# What the conftest sample's sample.json declares.
SAMPLE_TASK = "test_task"
SAMPLE_IMAGE = "test_task.tar"


@pytest.fixture(autouse=True)
def _isolated_config(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redirect the per-user config dir to a tmp dir for every test.

    Keeps reads/writes from :mod:`scoring.config` (scorer email, task selection,
    guidance URLs, the change banner) off the developer's real config and makes
    ``drain_changes()`` deterministic.
    """
    config_home = tmp_path_factory.mktemp("xdg-config")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))


def _make_sample_dir(root: Path, uuid: str, task: str, image: str) -> None:
    """Create ``<root>/<uuid>/project/`` and a minimal ``sample.json``."""
    import json

    sample_dir = root / uuid
    (sample_dir / "project").mkdir(parents=True, exist_ok=True)
    (sample_dir / "sample.json").write_text(
        json.dumps({"task": task, "image": image}), encoding="utf-8"
    )


def _build_state(samples_dir: Path) -> ScoringState:
    """Minimal valid ScoringState with one open sample."""
    info = local_sample_info(SAMPLE_UUID, samples_dir)
    state: ScoringState = {
        "queue": [
            {
                "uuid": SAMPLE_UUID,
                "task_name": SAMPLE_TASK,
                "status": "open",
            }
        ],
        "current_idx": 0,
        "uuid": SAMPLE_UUID,
        "info": info,
        "container_name": "manual-score-test",
        "setup_state": {"status": "ready"},
        "scorer_email": SCORER_EMAIL,
        "samples_dir": samples_dir,
        "images_dir": samples_dir.parent / "images",
        "sample_ids": [SAMPLE_UUID],
        "cli_image": "",
        "no_container": True,
        "cli_no_container": True,
        "attach": False,
        "setup_fn": lambda: None,
        "skipped": set(),
        "include_skipped": False,
        "include_done": False,
        "all_tasks": [SAMPLE_TASK],
        "task_counts": {SAMPLE_TASK: 1},
    }
    return state


def _make_client(state: ScoringState) -> FlaskClient:
    """Build a Flask test client over ``state`` with stub callbacks.

    The callbacks are inert stubs since no test drives queue navigation /
    container setup; routes write to ``history.jsonl`` directly via the store.
    """
    lock = threading.Lock()
    flask_app = app_module.create_app(
        state=state,
        lock=lock,
        advance_fn=lambda: "advanced",
        rebuild_queue_fn=lambda: None,
    )
    flask_app.config.update(TESTING=True)
    return flask_app.test_client()


@pytest.fixture
def samples_dir(tmp_path: Path) -> Path:
    """Tmp samples root with the test sample's dir + sample.json already set up."""
    _make_sample_dir(tmp_path, SAMPLE_UUID, SAMPLE_TASK, SAMPLE_IMAGE)
    return tmp_path


@pytest.fixture
def artifacts_dir(samples_dir: Path) -> Path:
    """Alias for :func:`samples_dir` (the store calls this root the samples dir)."""
    return samples_dir


@pytest.fixture
def client(samples_dir: Path) -> Iterator[FlaskClient]:
    """Flask test client backed by the on-disk JSONL store."""
    state = _build_state(samples_dir)
    with _make_client(state) as c:
        yield c
