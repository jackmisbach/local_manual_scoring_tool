"""Generate the canonical ``scores.jsonl`` export from ``history.jsonl``.

``scores.jsonl`` is a pure downstream export (METR imports it into a multi-author
warehouse); the tool never reads it back. Rather than double-writing it on every
submit, it is *rebuilt* on demand from each sample's history in
``history.jsonl`` — one flat row per history entry across all samples.

This is the join layer: it pulls each sample's working state from
:mod:`scoring.store` and its ``task_name`` from :mod:`scoring.state`, keeping
``store`` free of any dependency on ``state``.
"""

import json
from pathlib import Path

from scoring.state import load_sample_json, sample_task_name
from scoring.store import (
    build_score_row,
    iter_score_files,
    scores_path,
)


def build_score_rows(samples_dir: Path) -> list[dict[str, object]]:
    """Build every ``scores.jsonl`` row from ``history.jsonl`` history.

    Walks every sample with score content, joining each history entry with its
    sample's ``task_name`` (from ``sample.json``). Row order follows the records
    in ``history.jsonl`` and, within a sample, its history order.
    """
    rows: list[dict[str, object]] = []
    for uuid, score_file in iter_score_files(samples_dir):
        task_name = sample_task_name(load_sample_json(samples_dir, uuid))
        rows.extend(build_score_row(uuid, entry, task_name) for entry in score_file["history"])
    return rows


def export_scores(samples_dir: Path) -> Path:
    """Rebuild ``scores.jsonl`` from ``history.jsonl``; return the written path.

    Overwrites any existing file so the export always reflects current history
    (re-running it is idempotent). Always writes the file, even when there are no
    rows yet (an empty file is a valid "no scores" export).
    """
    rows = build_score_rows(samples_dir)
    path = scores_path(samples_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True))
            f.write("\n")
    return path
