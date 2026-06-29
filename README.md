# local-manual-scoring-tool

Tool for external people to manually score agent runs.

It: 
1. Loads data from a `data` dir
2. Starts a docker container with the agents output inside, and opens VSCode for you to look at it
3. Opens up a web interface where you can record your scores (<http://localhost:9090>)

## Prerequisites

- Docker desktop
- uv 
- VSCode (you can run `code`)

## Install

```bash
uv tool install git+https://github.com/METR/local_manual_scoring_tool
```

Copy paste `data.zip` from [this gdrive](https://drive.google.com/drive/folders/1LLhzKMvhB6k7fDZq5sI9KkTmsxm46F35?dmr=1&ec=wgc-drive-globalnav-goto) into the root of this repo, then unzip it with the password you were given:

```bash
unzip data.zip
```

Then run 
```bash
local-manual-score
```

## Exporting scores

When you're done scoring, export your records and send us the resulting
`output_scores.zip`:

```bash
local-manual-score export data/samples
```

This (re)builds `data/samples/output_score_logs/<sample>/score_log.jsonl` — one
event log per sample (your score events plus any invalidations) — and zips them
all into `data/samples/output_scores.zip`.

## Extras

### Config

Config path is `~/.config/local-manual-scoring-tool/config.json`

```bash
local-manual-score config show
```

| Key                          | Description                                                                                       |
| ---------------------------- | ------------------------------------------------------------------------------------------------- |
| `samples_dir`                | Default samples dir when none is passed on the CLI.                                                |
| `images_dir`                 | Default images dir (otherwise `<samples-dir>/../images`).                                          |
| `scorer_email`               | Your scorer identity (resolved + persisted on first run).                                          |
| `browser_command`            | Shell command to open the UI URL (URL appended last, or substituted into a `{url}` placeholder).   |
| `clear_on_submit`            | What the form clears after submit: `all`, `none`, `scores` (default), or `text`.                   |
| `pause_timer_hotkey`         | `KeyboardEvent.key` value that toggles the scoring timer (default `Pause`; empty disables).        |

### CLI flags

```bash
local-manual-score [SAMPLES_DIR] [options]
```

| Flag                | Description                                                                                   |
| ------------------- | --------------------------------------------------------------------------------------------- |
| `SAMPLES_DIR`       | Samples dir, or a single sample dir. Default: configured `samples_dir`, else `./data/samples`. |
| `--images-dir DIR`  | Where image tarballs live. Default: `<samples-dir>/../images`.                                 |
| `--image TAG`       | Use this already-loaded local Docker tag for all samples (overrides each `sample.json` image). |
| `--port N`          | Scoring UI port (default: 9090).                                                              |
| `--gui`             | Force noVNC for all samples (auto-detected for `*_gui_*`).                                     |
| `--no-container`    | Skip image load, container start, and VS Code launch (UI testing).                            |
| `--no-attach`       | Start containers but don't auto-attach VS Code.                                               |
| `--include-skipped` | Re-surface previously skipped samples.                                                        |
| `--include-done`    | Re-surface samples marked done.                                                               |
| `--scorer EMAIL`    | Scorer identity for this run.                                                                 |

Scorer identity resolves in order: `--scorer` > config `scorer_email` >
`git config user.email` > your username.

### GUI tasks (noVNC)

Tasks whose name contains `_gui_` (or ends in `_gui`) get noVNC set up
automatically in the container; you can also force it for all samples with
`--gui`. View the display at <http://localhost:6080/vnc.html>. Run GUI apps
inside the container with:

```bash
DISPLAY=:99 SDL_VIDEODRIVER=x11 XDG_RUNTIME_DIR=/tmp python main.py
```