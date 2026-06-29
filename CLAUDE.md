# CLAUDE.md

Orientation for AI sessions working in this repo.

## What this is

`local-manual-scoring-tool` — a **local-only** fork of METR/manual-scoring-tool.
It serves a Flask web UI (<http://localhost:9090>) for manually scoring agent
runs that are **already staged on disk**. Console command: `local-manual-score`
(see `[project.scripts]` in `pyproject.toml`; hatchling builds the `scoring`
package).

Key invariants (do not reintroduce what was stripped):

- **No Hawk, no AWS/S3, no ECR, no network sync, no CI screenshots.** The only
  place that intentionally talks to Hawk/AWS/ECR is `dev/` (staging utilities).
- **Blind-only.** The model name is never recorded or shown — there is no model
  field anywhere in the tool (not in `sample.json`, the queue, or the exported `score_log.jsonl`).
- **Images are tarballs loaded on demand.** No `docker pull`. The tool reads a
  `docker save` tarball's `RepoTag` and `docker load`s it if not already present.
- **Outputs:** all scoring + working/UI state (including per-sample score
  `history` and `invalidations`) lives in `history.jsonl`. The export is
  **generated on demand** by `local-manual-score export`: a per-sample
  `output_score_logs/<uuid>/score_log.jsonl` event log (score events +
  invalidation events) zipped to `output_scores.zip` — never written on submit,
  never read back by the tool. There are no per-sample `_score.json` files and no
  `tracking.json` (collapsed into `history.jsonl`), and no flat `scores.jsonl`
  (replaced by the score-log zip).

## Module map

- `scoring/__main__.py` — `main()` CLI entry point. Arg parsing, sample
  discovery, queue building (`build_local_queue`, sorted by task and shuffled
  within task groups), per-sample container setup in a background thread
  (`_setup_sample`), and `advance_to_next`. Also routes the `config` and
  `export` subcommands.
- `scoring/app.py` — `create_app()`: Flask routes / JSON API for the scoring UI
  (scoring page, submit, skip, done, invalidate, timers, task
  selection, exit).
- `scoring/state.py` — TypedDicts for the shared `ScoringState` / `QueueEntry` /
  `SampleInfo`, plus `sample.json` loading (`load_sample_json`,
  `local_sample_info`). Recognized `sample.json` keys: `task` (or `task_name`),
  `image`, and optionally `task_variant` / `project_dir` /
  `scoring_instructions_url` (the task-specific scoring instructions doc URL). A
  sample's `uuid` is its dir name, not a `sample.json` field.
- `scoring/store.py` — the on-disk JSONL store (see below). The working/UI view
  plus the persisted `skipped` flag over `history.jsonl` (status is derived via
  `derive_queue_status`), per-score `local_event_uuid` (`local_event_uuid_of`),
  invalidations (`add_invalidation`, `invalidated_local_uuids`), the pure
  score-log event builders (`build_score_event`, `build_invalidation_event`,
  `score_log_lines`), and the read-only `iter_score_files` walker.
- `scoring/export.py` — `export_score_logs(samples_dir)`: (re)builds a per-sample
  `output_score_logs/<uuid>/score_log.jsonl` event log (score + invalidation
  events) from `history.jsonl` and zips it to `output_scores.zip`. Backs
  `local-manual-score export`.
- `scoring/infra.py` — Docker + image loading. `ensure_task_image` (read tag
  from tarball manifest, reuse-or-`docker load`), `start_container` (bind-mounts
  the sample's `project/`, forwards ports, installs `requirements.txt`),
  `open_vscode_in_container`. No Hawk/AWS here.
- `scoring/config.py` — per-user config at
  `~/.config/local-manual-scoring-tool/config.json` (honors `$XDG_CONFIG_HOME`),
  the resolved accessors (samples/images dir, scorer, guidance URLs), and the
  `config show|path|edit|set|unset` subcommand.
- `scoring/task_selection.py` — global per-task enable/disable map, persisted in
  the user config under `task_selection`.
- `scoring/templates/` — Jinja templates: `scoring.html` (main UI),
  `summary.html` (end-of-queue), `task_selection.html`, `broken.html`, partials.

## Data layout

By default `data/samples/` (samples) and `data/images/` (image tarballs) sit
side by side. A sample is a `<uuid>/` dir containing `project/` (the agent's
working tree, bind-mounted into the container) and an optional `sample.json`.

JSONL / state files at the samples root:

- `output_score_logs/<uuid>/score_log.jsonl` + `output_scores.zip` — the export,
  **generated on demand** by `local-manual-score export` (never written on
  submit, never read back). One JSON event per line: `event_type:"score"` events
  (carrying `score.metadata.local_event_uuid`) and `event_type:"info"` /
  `info_source:"event_invalidation"` events (carrying `local_target_event_uuid`,
  with `target_event_uuid` left null for the DB importer to resolve). The whole
  `output_score_logs/` tree is zipped to `output_scores.zip`.
- `history.jsonl` — one line per sample (keyed by `id`), holding the persisted
  `skipped` flag plus the working/UI subset (`history`, `stash`, `finished_at`,
  `timers`, `invalidations`). Each finalized `history` entry carries
  a `score.metadata.local_event_uuid`; `invalidations` records which of those the
  user invalidated (`local_target_event_uuid`, `author`, `reason`,
  `invalidated_at`). Status is derived from this content, not stored.
  Read-modify-rewrite under a module lock.

Image tarballs live in the images dir as `<task>-<version>.tar` (named by
`dev/download_starting_image.py`). A sample's `sample.json` `image` field is the
**filename** of its tarball; the tarball's internal `RepoTag` is the Docker tag
the tool ultimately runs.

Status taxonomy: `open` -> `scored` (≥1 history entry) -> `done` (user clicked
Done Scoring); `skipped` is set explicitly. `running` / `ignored` exist only on
synthetic queue entries. The upstream `pending_score` / `pending_done` statuses
are never produced locally (every entry is born local).

## Conventions

- Python ≥ 3.11, ruff (line length 100) + pyright strict. `dev/`, `data/`,
  `tmp/`, `local/` are excluded from lint/type checks.
- Stdlib + Flask + markdown-it only; no heavyweight deps.
