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

Copy paste `data.zip` from [this gdrive](https://drive.google.com/drive/folders/1LLhzKMvhB6k7fDZq5sI9KkTmsxm46F35?dmr=1&ec=wgc-drive-globalnav-goto) into the root of this repo, then unzip it:

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

This will produce a zip file you can give to us in `data/samples/output_scores.zip`.

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
| `--no-container`    | Skip image load, container start, and VS Code launch (UI testing).                            |
| `--no-attach`       | Start containers but don't auto-attach VS Code.                                               |
| `--include-skipped` | Re-surface previously skipped samples.                                                        |
| `--include-done`    | Re-surface samples marked done.                                                               |
| `--scorer EMAIL`    | Scorer identity for this run.                                                                 |

Scorer identity resolves in order: `--scorer` > config `scorer_email` >
`git config user.email` > your username.