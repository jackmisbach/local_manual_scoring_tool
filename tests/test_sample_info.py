"""``sample.json`` -> ``SampleInfo`` loading, focused on ``scoring_instructions_url``."""

import json
from pathlib import Path

from scoring.state import local_sample_info


def _write_sample(root: Path, uuid: str, data: dict[str, object]) -> None:
    (root / uuid / "project").mkdir(parents=True, exist_ok=True)
    (root / uuid / "sample.json").write_text(json.dumps(data), encoding="utf-8")


def test_scoring_instructions_url_read_from_sample_json(tmp_path: Path) -> None:
    _write_sample(tmp_path, "s1", {"task": "t", "scoring_instructions_url": "https://docs/x"})
    info = local_sample_info("s1", tmp_path)
    assert info["scoring_instructions_url"] == "https://docs/x"


def test_scoring_instructions_url_defaults_empty(tmp_path: Path) -> None:
    _write_sample(tmp_path, "s2", {"task": "t"})
    info = local_sample_info("s2", tmp_path)
    assert info["scoring_instructions_url"] == ""
