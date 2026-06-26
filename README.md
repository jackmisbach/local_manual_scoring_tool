# local-manual-scoring-tool

A local Flask web UI for **manually scoring agent runs that are already staged on
disk**. It starts a Docker container with the sample's `project/` directory
bind-mounted, attaches VS Code to it (if the `code` CLI is on your PATH), and
serves a scoring form at <http://localhost:9090>.

This is a stripped-down, local-only fork: **no Hawk, no AWS/S3, no network sync.**
Everything it reads and writes lives under a local `data/` directory.

## Prerequisites

- **Docker Desktop** — must be running (the tool `docker load`s task images and
  starts containers). Pass `--no-container` to run the UI without Docker for
  testing.
- **uv** — to manage the venv and run / install the tool.
- **VS Code `code` CLI** (optional) — if `code` is on your PATH the tool
  auto-attaches VS Code into the running container. Without it, scoring still
  works; you just don't get the editor attach.

## Install

### Run from a clone (dev)

```bash
uv sync
uv run local-manual-score data/samples     # explicit samples dir
uv run local-manual-score                  # use the default / configured samples dir
```

### Install as a tool

```bash
uv tool install .                          # from a checkout
# or
uv tool install git+https://github.com/METR/local_manual_scoring_tool
```

Then run `local-manual-score` directly. For local development against an
editable checkout:

```bash
uv tool install --editable .
```

## On-disk layout

By default the tool works against a `data/` directory with sibling `samples/`
and `images/` dirs:

```
data/
├── samples/
│   ├── <uuid>/
│   │   ├── project/        # the agent's working tree (bind-mounted into the container)
│   │   └── sample.json     # optional per-sample metadata (see below)
│   ├── <uuid>/
│   │   └── project/
│   ├── scores.jsonl        # append-only finalized scores (output)
│   ├── ui_state.jsonl      # per-sample working/UI state (output)
│   └── .task_scratchpads.json
└── images/
    └── <task>-<version>.tar  # docker save image archive(s)
```

A sample is any immediate subdirectory of the samples dir that contains a
`project/` directory (an `artifacts/` dir is also accepted). You can also point
the tool at a single sample dir directly.

### `sample.json`

Optional, lives inside each `<uuid>/`. All fields are optional — scoring works
without it, but you then must supply the image via `--image`.

```json
{
  "run_uuid": "...",
  "task": "<task name>",
  "image": "<image-tarball-filename>"
}
```

- `task` — selects the task-specific guidance link and groups/sorts the queue.
- `image` — the **filename** of the image tarball inside the images dir
  (e.g. `sunlight-0_1_0.tar`). It must match the tarball produced when staging
  the image.

## Staging task images

Container images must already be available locally as `docker save` tarballs in
the images dir (or already loaded into Docker and selected with `--image`).
**There is no `docker pull`.**

To stage an image, use the developer utility (which is the one place that talks
to Hawk/AWS/ECR):

```bash
./dev/download_starting_image.py <sample-uuid> [<uuid> ...]
```

It writes `data/images/<task>-<version>.tar`. Set each sample's `sample.json`
`image` field to that tarball's filename. See [`dev/README.md`](dev/README.md).

### How images load

When a sample is opened, the tool looks up its tarball (`images_dir/<image>`),
reads the image's `RepoTag` from the tarball's manifest, and:

- reuses the image if that tag is already present in Docker, otherwise
- runs `docker load` on the tarball.

Samples that share the same tarball only load the image once.

## Guidance links

The scoring page shows two links: a **general scoring guidance** doc and a
**task-specific** one for the sample's `task`. Set them via config:

```bash
local-manual-score config set general_guidance_url "https://docs.example/guidance"
local-manual-score config set task_specific_guidance_url.<task> "https://docs.example/<task>"
```

The task-specific map is keyed by task name (the nested `...url.<task>` form sets
one entry).

## Scoring workflow

1. The tool discovers every sample under the samples dir, builds a queue sorted
   by task (and shuffled within each task group), and opens the UI.
2. For the current sample it loads the image, starts the container with
   `project/` bind-mounted, and (if `code` is available) attaches VS Code.
3. You fill in the score, minutes spent, and an explanation, then **Submit**.
   - **Next Sample** advances to the next unscored sample.
   - **Skip** skips the current sample (re-surface later with `--include-skipped`).
   - **Done Scoring This Sample** marks it finished (re-surface with `--include-done`).
4. Each finalized submission appends one line to `data/samples/scores.jsonl`.
   Working state (drafts, timers, scratchpad, status) lives in
   `data/samples/ui_state.jsonl`, so quitting and re-running resumes where you
   left off.

The model name is always hidden (the tool is **blind-only** — there is no
unblind control).

## GUI tasks (noVNC)

Tasks whose name contains `_gui_` (or ends in `_gui`) get noVNC set up
automatically in the container; you can also force it for all samples with
`--gui`. View the display at <http://localhost:6080/vnc.html>. Run GUI apps
inside the container with:

```bash
DISPLAY=:99 SDL_VIDEODRIVER=x11 XDG_RUNTIME_DIR=/tmp python main.py
```

## CLI flags

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

## Configuration

Config lives at `~/.config/local-manual-scoring-tool/config.json` (honors
`$XDG_CONFIG_HOME`). Inspect or change it with the `config` subcommand:

```bash
local-manual-score config show              # path + contents
local-manual-score config path              # path only
local-manual-score config edit              # open in $EDITOR
local-manual-score config set KEY VALUE
local-manual-score config unset KEY
```

| Key                          | Description                                                                                       |
| ---------------------------- | ------------------------------------------------------------------------------------------------- |
| `samples_dir`                | Default samples dir when none is passed on the CLI.                                                |
| `images_dir`                 | Default images dir (otherwise `<samples-dir>/../images`).                                          |
| `scorer_email`               | Your scorer identity (resolved + persisted on first run).                                          |
| `browser_command`            | Shell command to open the UI URL (URL appended last, or substituted into a `{url}` placeholder).   |
| `clear_on_submit`            | What the form clears after submit: `all`, `none`, `scores` (default), or `text`.                   |
| `pause_timer_hotkey`         | `KeyboardEvent.key` value that toggles the scoring timer (default `Pause`; empty disables).        |
| `general_guidance_url`       | General scoring guidance doc link shown on every scoring page.                                     |
| `task_specific_guidance_url` | Per-task guidance links; set one with `config set task_specific_guidance_url.<task> <url>`.        |

(The UI's Tasks button also persists a `task_selection` map here, controlling
which tasks appear in the queue.)
