# dev

Developer utilities for staging task data on disk for the local manual scoring tool.

Shared Hawk-API / sample-resolution helpers live in `_common.py`; both scripts import
it (each script runs standalone — Python puts `dev/` on the import path).

## `download_sample.py`

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
