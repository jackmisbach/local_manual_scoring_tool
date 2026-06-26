"""Tests for ``_install_project_deps``' batch-then-per-package fallback.

The real work happens in a container via ``docker exec``; what we can and
should test in isolation is the control flow and the parsing of the
per-package fallback's ``__OK__``/``__SKIPPED__`` markers. ``subprocess.run``
is stubbed with a dispatcher keyed on the command so each of the three calls
(requirements.txt presence check, batch install, per-package fallback)
returns a canned result.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from scoring import infra

_REQ_PRESENT = 0
_REQ_ABSENT = 1


def _make_run(
    *,
    req_present: bool = True,
    batch_rc: int = 0,
    fallback_stdout: str = "",
    fallback_stderr: str = "",
) -> Callable[..., SimpleNamespace]:
    """Build a fake ``subprocess.run`` that dispatches on the command.

    - ``test -f .../requirements.txt`` -> presence check
    - ``pip install ... -r ...``        -> batch install
    - the per-package ``bash -c`` loop  -> fallback (returns the canned markers)
    """

    def fake_run(cmd: list[str], *_args: Any, **_kwargs: Any) -> SimpleNamespace:
        if "test" in cmd and "-f" in cmd:
            return SimpleNamespace(
                returncode=_REQ_PRESENT if req_present else _REQ_ABSENT,
                stdout="",
                stderr="",
            )
        script = cmd[-1]
        if script.startswith("pip install") and "-r " in script:
            return SimpleNamespace(returncode=batch_rc, stdout="batch out", stderr="batch err")
        # per-package fallback loop
        assert "__OK__" in script
        assert "__SKIPPED__" in script
        return SimpleNamespace(returncode=0, stdout=fallback_stdout, stderr=fallback_stderr)

    return fake_run


def test_no_requirements_file_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", _make_run(req_present=False))
    assert infra._install_project_deps("c", "/proj") == []  # pyright: ignore[reportPrivateUsage]


def test_batch_success_skips_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", _make_run(batch_rc=0))
    assert infra._install_project_deps("c", "/proj") == []  # pyright: ignore[reportPrivateUsage]


def test_fallback_records_only_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    # Batch fails; fallback installs three, skips one. Only the skipped one
    # is returned, parsed from the __SKIPPED__ marker.
    stdout = (
        "__OK__ numpy==1.26.0\n" "__OK__ requests\n" "__SKIPPED__ pynput==1.7.6\n" "__OK__ rich\n"
    )
    monkeypatch.setattr(subprocess, "run", _make_run(batch_rc=1, fallback_stdout=stdout))
    assert infra._install_project_deps("c", "/proj") == ["pynput==1.7.6"]  # pyright: ignore[reportPrivateUsage]


def test_fallback_all_skipped_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing installed at all -> genuinely broken env, must hard-fail so the
    # UI surfaces it rather than silently launching an empty container.
    stdout = "__SKIPPED__ pynput==1.7.6\n__SKIPPED__ evdev\n"
    monkeypatch.setattr(subprocess, "run", _make_run(batch_rc=1, fallback_stdout=stdout))
    with pytest.raises(RuntimeError, match="pip install failed"):
        infra._install_project_deps("c", "/proj")  # pyright: ignore[reportPrivateUsage]


def test_fallback_pip_noise_does_not_become_a_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Lines without our markers (stray pip output) are ignored entirely.
    stdout = "Some unrelated pip line\n__OK__ numpy\n__SKIPPED__ pynput\n"
    monkeypatch.setattr(subprocess, "run", _make_run(batch_rc=1, fallback_stdout=stdout))
    assert infra._install_project_deps("c", "/proj") == ["pynput"]  # pyright: ignore[reportPrivateUsage]
