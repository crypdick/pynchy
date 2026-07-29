"""Behavior tests for the public ``pynchy build`` command."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

import pytest
from conftest import make_settings

from pynchy import __main__ as cli

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _Runtime:
    cli: str = "container-runtime"

    def __post_init__(self) -> None:
        self.ensure_running = Mock()


@dataclass(frozen=True)
class _RunResult:
    returncode: int


def _invoke_build(monkeypatch: pytest.MonkeyPatch, settings: object) -> int:
    monkeypatch.setattr("pynchy.config.api.get_settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["pynchy", "build"])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    return exited.value.code


def test_build_cli_rejects_a_project_without_an_agent_dockerfile(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    assert _invoke_build(monkeypatch, make_settings(project_root=tmp_path)) == 1

    assert capsys.readouterr().err == (
        f"Error: No Dockerfile at {tmp_path / 'src/pynchy/agent/Dockerfile'}\n"
    )


def test_build_cli_builds_and_cleans_the_selected_runtime(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    container_dir = tmp_path / "src" / "pynchy" / "agent"
    container_dir.mkdir(parents=True)
    (container_dir / "Dockerfile").write_text("FROM scratch\n")
    settings = make_settings(project_root=tmp_path)
    runtime = _Runtime()

    with (
        patch("pynchy.plugins.runtimes.detection.configure_runtime_override") as configure_runtime,
        patch("pynchy.plugins.runtimes.detection.get_runtime", return_value=runtime),
        patch(
            "pynchy.plugins.runtimes.cleanup.cleanup_runtime_build_state",
            side_effect=[True, True],
        ) as cleanup,
        patch("pynchy.__main__.subprocess.run", return_value=_RunResult(returncode=0)) as run,
    ):
        assert _invoke_build(monkeypatch, settings) == 0

    assert capsys.readouterr().out == (
        f"Building {settings.container.image} with container-runtime...\n"
    )
    configure_runtime.assert_called_once_with(settings.container.runtime)
    runtime.ensure_running.assert_called_once_with()
    run.assert_called_once_with(
        ["container-runtime", "build", "-t", settings.container.image, "."],
        cwd=str(container_dir),
        check=False,
    )
    assert cleanup.call_count == 2
