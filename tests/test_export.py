"""Tests for the on-demand ``scores.jsonl`` export (scoring.export).

``scores.jsonl`` is no longer written on submit; it is rebuilt from each
sample's history in ``history.jsonl`` by :func:`scoring.export.export_scores`
(and the ``local-manual-score export`` subcommand). These tests round-trip:
submit/append several scores, run the export, and assert the flat rows match the
underlying history. All I/O is against a tmp dir — no Docker, network, or Hawk.
"""

from __future__ import annotations

import json
from pathlib import Path

from scoring.export import export_scores
from scoring.store import SCORES_FILENAME, SavedScore, append_history, set_skipped

UUID_A = "aaaaaaaaaaaaaaaaaaaaaa"
UUID_B = "bbbbbbbbbbbbbbbbbbbbbb"
SCORER = "alice@metr.org"


def _entry(value: float, minutes: float = 7.0, author: str = SCORER) -> SavedScore:
    return {
        "scorer": "manual_score",
        "score": {
            "value": value,
            "answer": None,
            "explanation": f"because {value}",
            "history": [],
            "metadata": {
                "author": author,
                "minutes_spent_scoring": minutes,
                "scored_at": "2026-06-25T00:00:00Z",
            },
        },
    }


def _write_sample_json(root: Path, uuid: str, task: str) -> None:
    sample_dir = root / uuid
    (sample_dir / "project").mkdir(parents=True, exist_ok=True)
    (sample_dir / "sample.json").write_text(json.dumps({"task": task}), encoding="utf-8")


def _rows(path: Path) -> list[dict[str, object]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_export_empty_writes_empty_file(tmp_path: Path) -> None:
    """No history anywhere -> an empty (but present) scores.jsonl."""
    path = export_scores(tmp_path)
    assert path == tmp_path / SCORES_FILENAME
    assert path.exists()
    assert _rows(path) == []


def test_export_one_row_per_history_entry(tmp_path: Path) -> None:
    _write_sample_json(tmp_path, UUID_A, "demo")
    append_history(tmp_path, UUID_A, _entry(0.4, minutes=10))
    append_history(tmp_path, UUID_A, _entry(0.9, minutes=20))

    path = export_scores(tmp_path)
    rows = _rows(path)
    assert len(rows) == 2
    for row in rows:
        assert set(row) == {
            "id",
            "scorer",
            "value",
            "explanation",
            "minutes_to_score",
            "scored_at",
            "task_name",
        }
        assert row["id"] == UUID_A
        assert row["scorer"] == SCORER
        assert row["task_name"] == "demo"
    assert [r["value"] for r in rows] == [0.4, 0.9]
    assert [r["minutes_to_score"] for r in rows] == [10, 20]


def test_export_spans_multiple_samples(tmp_path: Path) -> None:
    _write_sample_json(tmp_path, UUID_A, "task_a")
    _write_sample_json(tmp_path, UUID_B, "task_b")
    append_history(tmp_path, UUID_A, _entry(0.1))
    append_history(tmp_path, UUID_B, _entry(0.2))
    append_history(tmp_path, UUID_B, _entry(0.3))

    rows = _rows(export_scores(tmp_path))
    assert len(rows) == 3
    by_id: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        by_id.setdefault(str(row["id"]), []).append(row)
    assert [r["value"] for r in by_id[UUID_A]] == [0.1]
    assert [r["value"] for r in by_id[UUID_B]] == [0.2, 0.3]
    assert {r["task_name"] for r in by_id[UUID_A]} == {"task_a"}
    assert {r["task_name"] for r in by_id[UUID_B]} == {"task_b"}


def test_export_is_idempotent_overwrite(tmp_path: Path) -> None:
    """Re-running export overwrites rather than appending."""
    _write_sample_json(tmp_path, UUID_A, "demo")
    append_history(tmp_path, UUID_A, _entry(0.5))
    export_scores(tmp_path)
    path = export_scores(tmp_path)
    assert len(_rows(path)) == 1


def test_export_ignores_skip_only_and_missing_task(tmp_path: Path) -> None:
    """Skip-only samples contribute no rows; missing sample.json -> empty task."""
    set_skipped(tmp_path, UUID_B)  # skip-only record, no history
    # UUID_A has history but no sample.json on disk.
    (tmp_path / UUID_A / "project").mkdir(parents=True, exist_ok=True)
    append_history(tmp_path, UUID_A, _entry(0.7))

    rows = _rows(export_scores(tmp_path))
    assert len(rows) == 1
    assert rows[0]["id"] == UUID_A
    assert rows[0]["task_name"] == ""
