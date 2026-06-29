# dev — staging data & developing the tool

Developer utilities and workflow for the [local manual scoring tool](../README.md).
The scoring tool scores samples that are **already staged on disk**; this directory
is how you stage that data (samples + their task images) and how you work on the
tool itself.

Staging is the one place that talks to **Hawk / AWS / ECR** — the scoring tool
itself never does. Shared Hawk-API / sample-resolution helpers live in `_common.py`;
both scripts import it (each runs standalone — Python puts `dev/` on the import path).

## Things users need to use local-manual-scoring-tool

1. Read access to this repo
2. Read access to [this gdrive](https://drive.google.com/drive/folders/1LLhzKMvhB6k7fDZq5sI9KkTmsxm46F35?dmr=1&ec=wgc-drive-globalnav-goto)
3. Instructions to send the logs exported using `local-manual-score export data/samples` to us.

## Adding a new run to score

To add a new run to score you will need to 
1. Download the sample from the db using `download_sample.py`
2. (If the sample uses an image not already in data/images), use `download_starting_image.py`
3. Add `scoring_instructions_url` field to the `data/samples/<sample_uuid>/samples.json` files, where the value is a link to a gdoc in [this gdrive](https://drive.google.com/drive/folders/1LLhzKMvhB6k7fDZq5sI9KkTmsxm46F35?dmr=1&ec=wgc-drive-globalnav-goto) with the task specific scoring instructions. 
4. Zip the new data dir and update the `data.zip` in [this gdrive](https://drive.google.com/drive/folders/1LLhzKMvhB6k7fDZq5sI9KkTmsxm46F35?dmr=1&ec=wgc-drive-globalnav-goto)

### `download_sample.py`

Stages one or more samples on disk for the local scoring tool. For each run (sample)
UUID it populates `data/samples/<uuid>/` with:

- `project/` — the agent's project directory, pulled from the sample's S3 artifacts
  (falls back to `artifacts/` for older samples).
- `sample.json` — `{"task": ..., "image": ...}`, where `image` is the
  task image's tarball name, e.g. `sunlight-0_1_0.tar` (the file
  `download_starting_image.py` produces). The sample's run UUID is the
  `<uuid>/` dir name itself.

### Usage

```bash
# Single UUID -> ./data/samples/<uuid>/{project/,sample.json}
./dev/download_sample.py FibrWqGmdXes26S5fqAsek

# Several UUIDs / from a file
./dev/download_sample.py uuid1 uuid2 uuid3
./dev/download_sample.py --from-file uuids.txt

# Choose the base dir and force a re-download of project/
./dev/download_sample.py uuid1 --output-dir data/samples --force
```

Needs the **hawk** and **AWS** CLIs (see prerequisites below; Docker is not required
for this script). If a UUID fails, the rest still run; the script exits non-zero and
lists the failures.

## `download_starting_image.py`

Downloads the unique **starting Docker images** for one or more run (sample) UUIDs.

Given the UUIDs it resolves each one's task name + version from the Hawk API
(`prd/inspect-tasks:{task_name}-{task_version}`), dedupes across all of them (many
samples share a task+version), then `docker pull`s each unique image and `docker save`s
it to a tarball **named by task + version** with `.` replaced by `_` — e.g.
`...:sunlight-0.1.0` → `sunlight-0_1_0.tar`. Load it elsewhere with
`docker load -i sunlight-0_1_0.tar`.

Tarballs are written to the scoring tool's images dir (`data/images/` by default),
so the scoring tool finds them automatically. **Each scored sample's `sample.json`
`image` field must equal the tarball filename** produced here (e.g.
`"image": "sunlight-0_1_0.tar"`) — that's the link between a sample and its image.
The script prints these filenames at the end as a reminder.

### Prerequisites

Same as METR/manual-scoring-tool:

- **hawk CLI** — logged in (`hawk login`)
- **AWS CLI** — TaskDeveloper SSO role (`aws sso login`)
- **Docker** — running

### Usage

```bash
# Single UUID -> ./data/images/<task-tag>.tar (e.g. ./data/images/sunlight-0_1_0.tar)
./dev/download_starting_image.py PRbujVHN8FabHpTrKrQvaE

# Several UUIDs (deduped to unique images)
./dev/download_starting_image.py uuid1 uuid2 uuid3

# From a file (one UUID per line, # comments allowed)
./dev/download_starting_image.py --from-file uuids.txt

# Choose output dir and force a re-pull
./dev/download_starting_image.py uuid1 --output-dir ./data/images --force-pull
```

If a UUID fails to resolve or an image fails to pull/save, the rest still run; the
script exits non-zero and lists the failures.

## How images load (at scoring time)

When the scoring tool opens a sample it looks up its tarball (`images_dir/<image>`),
reads the image's `RepoTag` from the tarball's manifest, and:

- reuses the image if that tag is already present in Docker, otherwise
- runs `docker load` on the tarball.

Samples that share the same tarball only load the image once. There is **no
`docker pull`**.

## Developing the tool

Run from a clone instead of `uv tool install`:

```bash
uv sync
uv run local-manual-score data/samples     # explicit samples dir
uv run local-manual-score                  # default / configured samples dir
```

For an editable global install: `uv tool install --editable .`.

### Checks

```bash
uv run ruff check scoring tests
uv run ruff format scoring tests
uv run pyright scoring tests
uv run pytest
```

Conventions: Python ≥ 3.11; ruff (line length 100) + pyright strict; stdlib +
Flask + markdown-it only (no heavyweight deps). `dev/`, `data/`, `tmp/`, and
`local/` are excluded from lint/type checks. See [`CLAUDE.md`](../CLAUDE.md) for a
module map and architecture orientation.

## Scoring workflow

1. The tool discovers every sample under the samples dir, builds a queue sorted
   by task (and shuffled within each task group), and opens the UI.
2. For the current sample it loads the image, starts the container with
   `project/` bind-mounted, and (if `code` is available) attaches VS Code.
3. You fill in the score, minutes spent, and an explanation, then **Submit**.
   - **Next Sample** advances to the next unscored sample.
   - **Skip** skips the current sample (re-surface later with `--include-skipped`).
   - **Done Scoring This Sample** marks it finished (re-surface with `--include-done`).
4. Every finalized submission is appended to the sample's score history in
   `data/samples/history.jsonl`, alongside working state (drafts, timers,
   scratchpad, status), so quitting and re-running resumes where you left off.
   The export (`data/samples/output_scores.zip`) is **not** written on submit —
   generate it on demand with `local-manual-score export` (see below).


### `sample.json`

Optional, lives inside each `<uuid>/`. All fields are optional — scoring works
without it, but you then must supply the image via `--image`.

```json
{
  "task": "<task name>",
  "image": "<image-tarball-filename>"
}
```

- `task` — selects the task-specific guidance link and groups/sorts the queue.
- `image` — the **filename** of the image tarball inside the images dir
  (e.g. `sunlight-0_1_0.tar`). It must match the tarball produced when staging
  the image.

## Staging samples and images

The tool only scores data that is **already on disk** — it never pulls from the
network. Samples (`<uuid>/project/` + `sample.json`) and their task images
(`docker save` tarballs in the images dir) are produced by a separate developer
step that talks to Hawk/AWS. See [`dev/README.md`](dev/README.md).

Images load on demand: the first time a sample using a given tarball is opened the
tool `docker load`s it (reusing the image if its tag is already present locally),
so there is **no `docker pull`** and samples sharing a tarball load only once.


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
│   ├── output_scores.zip   # score-log export (generated on demand by `export`)
│   └── history.jsonl       # per-sample working/UI state incl. score history (output)
└── images/
    └── <task>-<version>.tar  # docker save image archive(s)
```

A sample is any immediate subdirectory of the samples dir that contains a
`project/` directory (an `artifacts/` dir is also accepted). You can also point
the tool at a single sample dir directly.

