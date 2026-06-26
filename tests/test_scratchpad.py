"""Task/variant scratch-pad storage: precedence, save routes, and robustness.

Covers the on-disk store (``.task_scratchpads.json``), the seed precedence
the index route applies (per-sample > variant > task > ""), the promote-to-scope
save routes, and degradation when the dotfile is hand-edited or corrupt. The
store seeds the scratch pad on every page load, so a malformed entry must not
crash the loaders/savers (it would 500 the UI on load).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from flask.testing import FlaskClient

from scoring import app as app_module
from scoring.store import (
    TASK_SCRATCHPADS_FILENAME,
    clean_scratchpad_entry,
    load_task_scratchpad,
    load_task_scratchpads,
    load_variant_scratchpad,
    save_scratchpad,
    save_task_scratchpad,
    save_variant_scratchpad,
)
from tests.conftest import SAMPLE_UUID

TASK = "sunlight"
VARIANT = "hard"


def _info(task_name: str, task_variant: str) -> dict[str, Any]:
    return {"task_name": task_name, "task_variant": task_variant}


def _write_store(artifacts_dir: Path, raw: object) -> None:
    (artifacts_dir / TASK_SCRATCHPADS_FILENAME).write_text(json.dumps(raw), encoding="utf-8")


# --- storage + precedence -------------------------------------------------


def test_missing_store_loads_empty(artifacts_dir: Path) -> None:
    assert load_task_scratchpad(artifacts_dir, TASK) == ""
    assert load_variant_scratchpad(artifacts_dir, TASK, VARIANT) == ""


def test_task_roundtrip_uses_dotfile(artifacts_dir: Path) -> None:
    save_task_scratchpad(artifacts_dir, TASK, "task notes")
    assert load_task_scratchpad(artifacts_dir, TASK) == "task notes"
    assert (artifacts_dir / TASK_SCRATCHPADS_FILENAME).exists()


def test_variant_note_is_independent_of_task_note(artifacts_dir: Path) -> None:
    save_task_scratchpad(artifacts_dir, TASK, "task notes")
    save_variant_scratchpad(artifacts_dir, TASK, VARIANT, "variant notes")
    assert load_task_scratchpad(artifacts_dir, TASK) == "task notes"
    assert load_variant_scratchpad(artifacts_dir, TASK, VARIANT) == "variant notes"
    assert load_variant_scratchpad(artifacts_dir, TASK, "other") == ""


def test_clearing_task_keeps_variant(artifacts_dir: Path) -> None:
    save_task_scratchpad(artifacts_dir, TASK, "task notes")
    save_variant_scratchpad(artifacts_dir, TASK, VARIANT, "variant notes")
    save_task_scratchpad(artifacts_dir, TASK, "")
    assert load_task_scratchpad(artifacts_dir, TASK) == ""
    assert load_variant_scratchpad(artifacts_dir, TASK, VARIANT) == "variant notes"


def test_emptying_everything_removes_task_entry(artifacts_dir: Path) -> None:
    save_task_scratchpad(artifacts_dir, TASK, "task notes")
    save_task_scratchpad(artifacts_dir, TASK, "")
    assert TASK not in load_task_scratchpads(artifacts_dir)


@pytest.mark.parametrize(
    ("task_name", "variant"),
    [("", VARIANT), (TASK, "")],
)
def test_save_requires_scope_keys(artifacts_dir: Path, task_name: str, variant: str) -> None:
    with pytest.raises(ValueError, match="required to save a variant scratchpad"):
        save_variant_scratchpad(artifacts_dir, task_name, variant, "x")


@pytest.mark.parametrize(
    ("per_sample", "variant_note", "task_note", "expected"),
    [
        ("sample", "variant", "task", "sample"),
        ("", "variant", "task", "variant"),
        ("", "", "task", "task"),
        ("", "", "", ""),
    ],
)
def test_seed_precedence(
    artifacts_dir: Path,
    per_sample: str,
    variant_note: str,
    task_note: str,
    expected: str,
) -> None:
    if per_sample:
        save_scratchpad(artifacts_dir, SAMPLE_UUID, per_sample)
    if variant_note:
        save_variant_scratchpad(artifacts_dir, TASK, VARIANT, variant_note)
    if task_note:
        save_task_scratchpad(artifacts_dir, TASK, task_note)
    seed = app_module._scratchpad_seed(artifacts_dir, SAMPLE_UUID, _info(TASK, VARIANT))  # type: ignore[arg-type]
    assert seed == expected


# --- save routes ----------------------------------------------------------


def test_save_task_route_persists(client: FlaskClient, artifacts_dir: Path) -> None:
    resp = client.post("/api/scratchpad/task", json={"content": "promoted"})
    assert resp.get_json()["success"] is True
    # conftest's sample has task_name "test_task".
    assert load_task_scratchpad(artifacts_dir, "test_task") == "promoted"


def test_save_variant_route_rejects_blank_variant(client: FlaskClient) -> None:
    # conftest's sample has an empty task_variant, so the variant scope can't apply.
    resp = client.post("/api/scratchpad/variant", json={"content": "x"})
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False


# --- robustness against a malformed store ---------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "not even a json object",  # top-level JSON string
        ["a", "b"],  # top-level JSON array
        {TASK: "a bare string note"},  # entry is a string, not an object
        {TASK: 123},  # entry is a number
        {TASK: {"variants": "notadict"}},  # variants is not an object
        {TASK: {"task": 123}},  # task note is not a string
        {TASK: {"variants": {VARIANT: 5}}},  # variant note is not a string
    ],
)
def test_loaders_tolerate_malformed_store(artifacts_dir: Path, raw: object) -> None:
    _write_store(artifacts_dir, raw)
    # No accessor may raise, and each yields the empty fallback.
    assert load_task_scratchpad(artifacts_dir, TASK) == ""
    assert load_variant_scratchpad(artifacts_dir, TASK, VARIANT) == ""
    assert isinstance(load_task_scratchpads(artifacts_dir), dict)


@pytest.mark.parametrize(
    "raw",
    [
        {TASK: "a bare string note"},
        {TASK: {"variants": "notadict"}},
    ],
)
def test_seed_tolerates_malformed_store(artifacts_dir: Path, raw: object) -> None:
    _write_store(artifacts_dir, raw)
    seed = app_module._scratchpad_seed(artifacts_dir, SAMPLE_UUID, _info(TASK, VARIANT))  # type: ignore[arg-type]
    assert seed == ""


def test_savers_heal_a_malformed_store(artifacts_dir: Path) -> None:
    _write_store(artifacts_dir, {TASK: "a bare string note"})
    # The save path must not raise on a malformed pre-existing store...
    save_task_scratchpad(artifacts_dir, TASK, "fresh task note")
    save_variant_scratchpad(artifacts_dir, TASK, VARIANT, "fresh variant note")
    # ...and the store ends up well-formed and readable.
    assert load_task_scratchpad(artifacts_dir, TASK) == "fresh task note"
    assert load_variant_scratchpad(artifacts_dir, TASK, VARIANT) == "fresh variant note"


def test_clean_scratchpad_entry_drops_unusable() -> None:
    assert clean_scratchpad_entry("string") is None
    assert clean_scratchpad_entry(123) is None
    assert clean_scratchpad_entry({"task": 1, "variants": "x"}) == {}
    assert clean_scratchpad_entry({"task": "n", "variants": {"v": "ok", "bad": 2}}) == {
        "task": "n",
        "variants": {"v": "ok"},
    }
