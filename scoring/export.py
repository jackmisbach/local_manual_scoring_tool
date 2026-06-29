"""Generate the per-sample score-log export from ``history.jsonl``.

The export is a pure downstream artifact (METR imports it into a multi-author
warehouse); the tool never reads it back. Rather than double-writing on every
submit, it is *rebuilt* on demand from each sample's history in
``history.jsonl``:

- one ``score_log.jsonl`` per sample, under ``output_score_logs/<uuid>/``, with
  one event per line (score events then invalidation events — see
  :func:`scoring.store.score_log_lines`), and
- the whole tree zipped to ``output_scores.zip`` at the samples root, ready for
  DB import.

Sample association is the per-sample dir name (the ``<uuid>/`` containing each
``score_log.jsonl``); score events carry no ``task_name``, so this module needs
no dependency on :mod:`scoring.state`.
"""

import json
import shutil
from pathlib import Path

from scoring.store import iter_score_files, score_log_lines


def export_score_logs(samples_dir: Path) -> Path:
    """Rebuild the per-sample score logs and zip them; return the zip path.

    Rebuilds ``output_score_logs/`` from scratch (so deleted samples don't
    linger), writes ``output_score_logs/<uuid>/score_log.jsonl`` for every sample
    with score-log content, then zips the tree to ``output_scores.zip`` at the
    samples root. Re-running is idempotent. A stale ``scores.jsonl`` from the
    legacy export is cleaned up best-effort.
    """
    out_dir = samples_dir / "output_score_logs"
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    for uuid, score_file in iter_score_files(samples_dir):
        lines = score_log_lines(score_file, uuid)
        if not lines:
            continue
        sample_out = out_dir / uuid
        sample_out.mkdir(parents=True, exist_ok=True)
        with (sample_out / "score_log.jsonl").open("w", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line, sort_keys=True))
                f.write("\n")

    shutil.make_archive(str(samples_dir / "output_scores"), "zip", root_dir=str(out_dir))

    # Best-effort cleanup of the legacy flat export, if present.
    (samples_dir / "scores.jsonl").unlink(missing_ok=True)

    return samples_dir / "output_scores.zip"
