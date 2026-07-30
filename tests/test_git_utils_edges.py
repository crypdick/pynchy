"""Git helper process-boundary contracts."""

from __future__ import annotations

import subprocess  # noqa: S404 - tests exercise the helper's timeout contract.
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from pynchy.host.git_ops.api import run_git

if TYPE_CHECKING:
    from pathlib import Path


def test_run_git_requires_default_cwd_when_cwd_is_omitted(monkeypatch) -> None:
    monkeypatch.setattr("pynchy.host.git_ops.utils._default_cwd", None)

    with pytest.raises(RuntimeError, match="Git default working directory has not been configured"):
        run_git("status")


def test_run_git_timeout_survives_already_exited_process(monkeypatch, tmp_path: Path) -> None:
    process = Mock(pid=123, returncode=0)
    process.communicate.side_effect = subprocess.TimeoutExpired(["git", "status"], 0)
    monkeypatch.setattr(
        "pynchy.host.git_ops.utils.subprocess.Popen", lambda *args, **kwargs: process
    )
    monkeypatch.setattr(
        "pynchy.host.git_ops.utils.os.killpg",
        Mock(side_effect=ProcessLookupError),
    )

    result = run_git("status", cwd=tmp_path, timeout=0)

    assert result.returncode == 124
