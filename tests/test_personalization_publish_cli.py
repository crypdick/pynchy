"""Behavior tests for the host-only personalization publication command."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from conftest import make_settings

from pynchy import __main__ as cli
from pynchy.config.api import validate_personalization_configuration

if TYPE_CHECKING:
    from pathlib import Path


def _invoke_publish(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    project_root: Path,
    result: str,
) -> tuple[int, str, str]:
    settings = make_settings(project_root=project_root)
    monkeypatch.setattr("pynchy.config.api.get_settings", lambda: settings)
    monkeypatch.setattr("pynchy.personalization_cli._source_checkout_root", lambda: project_root)
    monkeypatch.chdir(project_root)
    monkeypatch.setattr(sys, "argv", ["pynchy", "publish-personalization"])

    with (
        patch("dotenv.load_dotenv") as load_dotenv,
        patch("pynchy.host.git_ops.api.configure_repo_runtime") as configure,
        patch(
            "pynchy.host.git_ops.api.sync_personalization_repo",
            return_value=result,
        ) as publish,
        pytest.raises(SystemExit) as exited,
    ):
        cli.main()

    assert publish.call_args.args[0] == project_root
    assert publish.call_args.args[1] is validate_personalization_configuration
    load_dotenv.assert_not_called()
    configured_get_settings = configure.call_args.kwargs["get_settings"]
    assert configured_get_settings() is settings
    assert configure.call_args.kwargs["resolve_workspace_config"]("unused") is None
    captured = capsys.readouterr()
    return exited.value.code, captured.out, captured.err


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ("pushed", "Personalization published.\n"),
        ("updated", "Personalization updated from origin.\n"),
        ("idle", "Personalization already matches origin.\n"),
    ],
)
def test_publish_personalization_cli_reports_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    result: str,
    expected: str,
) -> None:
    exit_code, output, errors = _invoke_publish(monkeypatch, capsys, tmp_path, result)

    assert exit_code == 0
    assert output == expected
    assert not errors


@pytest.mark.parametrize("result", ["failed", "skipped"])
def test_publish_personalization_cli_keeps_failures_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    result: str,
) -> None:
    exit_code, output, errors = _invoke_publish(monkeypatch, capsys, tmp_path, result)

    assert exit_code == 1
    assert not output
    assert "token" not in errors.lower()
    assert "credential" not in errors.lower()


def test_publish_personalization_cli_hides_publication_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    private_content = "private personalization value"
    settings = make_settings()
    monkeypatch.setattr("pynchy.personalization_cli._source_checkout_root", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("pynchy.config.api.get_settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["pynchy", "publish-personalization"])

    with (
        patch("dotenv.load_dotenv"),
        patch("pynchy.host.git_ops.api.configure_repo_runtime"),
        patch(
            "pynchy.host.git_ops.api.sync_personalization_repo",
            side_effect=ValueError(private_content),
        ),
        pytest.raises(SystemExit) as exited,
    ):
        cli.main()

    assert exited.value.code == 1
    captured = capsys.readouterr()
    assert private_content not in captured.out
    assert private_content not in captured.err


def test_publish_personalization_cli_hides_runtime_setup_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    private_content = "private runtime setup value"
    monkeypatch.setattr("pynchy.personalization_cli._source_checkout_root", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["pynchy", "publish-personalization"])

    with (
        patch(
            "pynchy.host.git_ops.api.configure_repo_runtime",
            side_effect=RuntimeError(private_content),
        ),
        patch("pynchy.host.git_ops.api.sync_personalization_repo") as publish,
        pytest.raises(SystemExit) as exited,
    ):
        cli.main()

    assert exited.value.code == 1
    publish.assert_not_called()
    captured = capsys.readouterr()
    assert private_content not in captured.out
    assert private_content not in captured.err


def test_publish_personalization_cli_rejects_non_checkout_working_directory(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "checkout"
    project_root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr("pynchy.personalization_cli._source_checkout_root", lambda: project_root)
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(sys, "argv", ["pynchy", "publish-personalization"])

    with (
        patch("pynchy.host.git_ops.api.configure_repo_runtime") as configure,
        patch("pynchy.host.git_ops.api.sync_personalization_repo") as publish,
        pytest.raises(SystemExit) as exited,
    ):
        cli.main()

    assert exited.value.code == 1
    configure.assert_not_called()
    publish.assert_not_called()
    captured = capsys.readouterr()
    assert not captured.out
    assert "checkout root" in captured.err


@pytest.mark.parametrize(
    "arguments",
    [
        ["data/another-repository"],
        ["--force"],
        ["--branch", "main"],
        ["--remote", "origin"],
    ],
)
def test_publish_personalization_cli_rejects_publication_selectors(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["pynchy", "publish-personalization", *arguments],
    )

    with pytest.raises(SystemExit) as exited:
        cli.main()

    assert exited.value.code == 2
