"""Public CLI tests for the isolated-runtime dependency installer."""

from __future__ import annotations

import subprocess  # noqa: S404 - test doubles only record dependency command results.
import sys
from pathlib import Path

import pytest
from scripts import install_new_feature_dependencies as installer


def _invoke_main(
    monkeypatch: pytest.MonkeyPatch,
    *args: str,
) -> None:
    monkeypatch.setattr(sys, "argv", ["install_new_feature_dependencies.py", *args])
    installer.main()


def _fake_tool_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    def command_exists(name: str) -> str | None:
        return {"docker": "/usr/bin/docker", "uv": "/usr/bin/uv"}.get(name)

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command == ["/usr/bin/docker", "info"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[-1] == "--version" and Path(command[0]).name == "temporal":
            return subprocess.CompletedProcess(
                command, 0, stdout="temporal version 1.8.0\n", stderr=""
            )
        if command[-1] == "--version" and Path(command[0]).name == "new-feature":
            return subprocess.CompletedProcess(command, 0, stdout="new-feature 1.1.6\n", stderr="")
        raise AssertionError(f"Unexpected dependency command: {command}")

    monkeypatch.setattr(installer.shutil, "which", command_exists)
    monkeypatch.setattr(installer.subprocess, "run", run)


def test_runtime_only_check_reports_healthy_selected_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "temporal").touch()
    _fake_tool_commands(monkeypatch)

    _invoke_main(monkeypatch, "--bin-dir", str(bin_dir), "--runtime-only", "--check")

    assert capsys.readouterr().out.splitlines() == [
        "Docker: ready",
        f"Temporal CLI: {bin_dir / 'temporal'}",
        f"Add {bin_dir} to PATH before running the deterministic runtime",
    ]


def test_runtime_only_check_fails_when_the_pinned_temporal_binary_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_tool_commands(monkeypatch)

    with pytest.raises(SystemExit, match="1"):
        _invoke_main(monkeypatch, "--bin-dir", str(tmp_path / "bin"), "--runtime-only", "--check")

    assert "Pinned Temporal CLI v1.8.0 is missing" in capsys.readouterr().err


def test_runtime_only_install_places_temporal_in_the_selected_bin_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bin_dir = tmp_path / "runtime-bin"
    installed: list[Path] = []
    _fake_tool_commands(monkeypatch)

    def install_temporal(destination: Path) -> Path:
        installed.append(destination)
        destination.mkdir()
        temporal = destination / "temporal"
        temporal.write_text("installed")
        return temporal

    monkeypatch.setattr(
        "scripts.install_new_feature_dependencies._install_temporal", install_temporal
    )

    _invoke_main(monkeypatch, "--bin-dir", str(bin_dir), "--runtime-only")

    assert installed == [bin_dir]
    assert (bin_dir / "temporal").read_text() == "installed"
    assert f"Temporal CLI: {bin_dir / 'temporal'}" in capsys.readouterr().out


def test_full_profile_check_accepts_selected_pinned_clis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for command in ("temporal", "new-feature", "codex"):
        (bin_dir / command).touch()
    _fake_tool_commands(monkeypatch)

    _invoke_main(monkeypatch, "--bin-dir", str(bin_dir), "--check")

    assert capsys.readouterr().out.splitlines() == [
        "Docker: ready",
        f"Temporal CLI: {bin_dir / 'temporal'}",
        f"new-feature: {bin_dir / 'new-feature'}",
        f"Codex CLI: {bin_dir / 'codex'}",
        f"Add {bin_dir} to PATH before running new-feature",
    ]


def test_full_profile_install_creates_missing_selected_clis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "temporal").touch()
    _fake_tool_commands(monkeypatch)
    installs: list[tuple[str, Path]] = []

    def install_new_feature(_uv: str, destination: Path) -> None:
        installs.append(("new-feature", destination))
        (destination / "new-feature").touch()

    def install_codex(_npm: str, destination: Path) -> None:
        installs.append(("codex", destination))
        (destination / "codex").touch()

    monkeypatch.setattr(
        "scripts.install_new_feature_dependencies._install_new_feature", install_new_feature
    )
    monkeypatch.setattr("scripts.install_new_feature_dependencies._install_codex", install_codex)

    def command_exists(name: str) -> str | None:
        if name == "npm":
            return "/usr/bin/npm"
        return {"docker": "/usr/bin/docker", "uv": "/usr/bin/uv"}.get(name)

    monkeypatch.setattr(installer.shutil, "which", command_exists)

    _invoke_main(monkeypatch, "--bin-dir", str(bin_dir))

    assert installs == [("new-feature", bin_dir), ("codex", bin_dir)]
    assert (bin_dir / "new-feature").is_file()
    assert (bin_dir / "codex").is_file()
    assert f"new-feature: {bin_dir / 'new-feature'}" in capsys.readouterr().out
