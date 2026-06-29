"""Tests for the on-demand per-sample score-log export (scoring.export).

The export is no longer written on submit; it is rebuilt from each sample's
history in ``history.jsonl`` by :func:`scoring.export.export_score_logs` (and the
``local-manual-score export`` subcommand). These tests round-trip: append a few
scores (and an invalidation), run the export, unzip ``output_scores.zip``, and
assert the per-sample ``score_log.jsonl`` lines. All I/O is against a tmp dir —
no Docker, network, or Hawk.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from scoring.export import export_score_logs
from scoring.store import SavedScore, add_invalidation, append_history, set_skipped

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
                "local_event_uuid": f"evt-{value}",
            },
        },
    }


def _zip_lines(zip_path: Path, uuid: str) -> list[dict[str, object]]:
    """Read the per-sample score_log.jsonl out of output_scores.zip."""
    with zipfile.ZipFile(zip_path) as zf:
        raw = zf.read(f"{uuid}/score_log.jsonl").decode("utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _zip_names(zip_path: Path) -> set[str]:
    """File entries in the zip (directory entries excluded)."""
    with zipfile.ZipFile(zip_path) as zf:
        return {name for name in zf.namelist() if not name.endswith("/")}


def test_export_empty_writes_zip(tmp_path: Path) -> None:
    """No history anywhere -> an empty (but present) output_scores.zip."""
    path = export_score_logs(tmp_path)
    assert path == tmp_path / "output_scores.zip"
    assert path.exists()
    assert _zip_names(path) == set()


def test_export_score_events_per_history_entry(tmp_path: Path) -> None:
    append_history(tmp_path, UUID_A, _entry(0.4, minutes=10))
    append_history(tmp_path, UUID_A, _entry(0.9, minutes=20))

    path = export_score_logs(tmp_path)
    assert f"{UUID_A}/score_log.jsonl" in _zip_names(path)
    lines = _zip_lines(path, UUID_A)
    assert len(lines) == 2
    for line in lines:
        assert line["event_type"] == "score"
        assert line["scorer"] == "manual_score"
        meta = line["score"]["metadata"]  # type: ignore[index]
        # Every score event carries its stored local_event_uuid.
        assert isinstance(meta["local_event_uuid"], str)  # type: ignore[index]
        assert meta["local_event_uuid"]  # type: ignore[index]
    assert [line["score"]["value"] for line in lines] == [0.4, 0.9]  # type: ignore[index]


def test_export_invalidation_event(tmp_path: Path) -> None:
    append_history(tmp_path, UUID_A, _entry(0.4))
    # Derive the score event's local uuid to target it.
    score_line = _zip_lines(export_score_logs(tmp_path), UUID_A)[0]
    target = score_line["score"]["metadata"]["local_event_uuid"]  # type: ignore[index]

    add_invalidation(tmp_path, UUID_A, target, SCORER, "wrong value")
    lines = _zip_lines(export_score_logs(tmp_path), UUID_A)
    info = [line for line in lines if line["event_type"] == "info"]
    assert len(info) == 1
    line = info[0]
    assert line["info_source"] == "event_invalidation"
    info_data = line["info_data"]
    assert info_data["target_event_uuid"] is None  # type: ignore[index]
    assert info_data["local_target_event_uuid"] == target  # type: ignore[index]
    prov = info_data["provenance"]  # type: ignore[index]
    assert prov["author"] == SCORER  # type: ignore[index]
    assert prov["reason"] == "wrong value"  # type: ignore[index]
    assert prov["metadata"]["tool"] == "local-manual-scoring-tool"  # type: ignore[index]


def test_export_spans_multiple_samples(tmp_path: Path) -> None:
    append_history(tmp_path, UUID_A, _entry(0.1))
    append_history(tmp_path, UUID_B, _entry(0.2))
    append_history(tmp_path, UUID_B, _entry(0.3))

    path = export_score_logs(tmp_path)
    assert _zip_names(path) == {
        f"{UUID_A}/score_log.jsonl",
        f"{UUID_B}/score_log.jsonl",
    }
    assert [line["score"]["value"] for line in _zip_lines(path, UUID_A)] == [0.1]  # type: ignore[index]
    assert [line["score"]["value"] for line in _zip_lines(path, UUID_B)] == [0.2, 0.3]  # type: ignore[index]


def test_export_is_idempotent(tmp_path: Path) -> None:
    """Re-running export rebuilds rather than appending."""
    append_history(tmp_path, UUID_A, _entry(0.5))
    export_score_logs(tmp_path)
    path = export_score_logs(tmp_path)
    assert len(_zip_lines(path, UUID_A)) == 1


def test_export_ignores_skip_only(tmp_path: Path) -> None:
    """Skip-only samples contribute no score log."""
    set_skipped(tmp_path, UUID_B)  # skip-only record, no history
    append_history(tmp_path, UUID_A, _entry(0.7))

    path = export_score_logs(tmp_path)
    assert _zip_names(path) == {f"{UUID_A}/score_log.jsonl"}


def test_export_cleans_stale_scores_jsonl(tmp_path: Path) -> None:
    """A leftover legacy scores.jsonl is removed by the export."""
    (tmp_path / "scores.jsonl").write_text("stale\n", encoding="utf-8")
    export_score_logs(tmp_path)
    assert not (tmp_path / "scores.jsonl").exists()
