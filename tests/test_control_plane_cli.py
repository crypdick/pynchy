"""Behavior tests for the control-plane credential CLI."""

from __future__ import annotations

import stat
import sys
from typing import TYPE_CHECKING

import pytest
from conftest import make_settings

from pynchy import __main__ as cli

if TYPE_CHECKING:
    from pathlib import Path

    from _pytest.capture import CaptureFixture
    from _pytest.monkeypatch import MonkeyPatch


def _run_bootstrap(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
    project_root: Path,
    *arguments: str,
) -> tuple[int, str, str]:
    settings = make_settings(project_root=project_root)
    monkeypatch.setattr("pynchy.config.api.get_settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["pynchy", "control-plane", "bootstrap", *arguments])

    with pytest.raises(SystemExit) as exited:
        cli.main()

    captured = capsys.readouterr()
    return exited.value.code, captured.out, captured.err


def test_control_plane_bootstrap_cli_creates_a_restricted_token(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
    tmp_path: Path,
) -> None:
    exit_code, output, errors = _run_bootstrap(monkeypatch, capsys, tmp_path)
    token_path = tmp_path / "data" / "control-plane.token"

    assert exit_code == 0
    assert not errors
    assert output == f"Created permission-restricted control-plane token: {token_path}\n"
    assert len(token_path.read_text().strip()) >= 32
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_control_plane_bootstrap_cli_rotates_an_existing_token(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
    tmp_path: Path,
) -> None:
    assert _run_bootstrap(monkeypatch, capsys, tmp_path)[0] == 0
    token_path = tmp_path / "data" / "control-plane.token"
    original_token = token_path.read_text()

    exit_code, output, errors = _run_bootstrap(monkeypatch, capsys, tmp_path, "--rotate")

    assert exit_code == 0
    assert not errors
    assert output == f"Rotated permission-restricted control-plane token: {token_path}\n"
    assert token_path.read_text() != original_token


def test_control_plane_bootstrap_cli_refuses_to_overwrite_without_rotate(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
    tmp_path: Path,
) -> None:
    assert _run_bootstrap(monkeypatch, capsys, tmp_path)[0] == 0
    token_path = tmp_path / "data" / "control-plane.token"

    exit_code, output, errors = _run_bootstrap(monkeypatch, capsys, tmp_path)

    assert exit_code == 1
    assert not output
    assert errors == (
        f"Control-plane bootstrap failed: Control-plane token already exists: {token_path}\n"
    )


@pytest.mark.parametrize(
    ("command", "relative_url", "method"),
    [("status", "/status", "GET"), ("deploy", "/deploy", "POST")],
)
def test_control_plane_command_uses_the_selected_authenticated_target(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
    tmp_path: Path,
    command: str,
    relative_url: str,
    method: str,
) -> None:
    socket_path = tmp_path / "pynchy.sock"
    token_file = tmp_path / "control-plane.token"
    request: dict[str, object] = {}

    def fetch(
        host: str | None,
        path: str,
        *,
        method: str,
        socket_path: Path | None,
        token_file: Path | None,
    ) -> object:
        request.update(
            host=host,
            path=path,
            method=method,
            socket_path=socket_path,
            token_file=token_file,
        )
        return {"ok": True}

    monkeypatch.setattr("pynchy.__main__._fetch_control_payload", fetch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pynchy",
            "--socket",
            str(socket_path),
            "--token-file",
            str(token_file),
            command,
        ],
    )

    with pytest.raises(SystemExit) as exited:
        cli.main()

    captured = capsys.readouterr()
    assert exited.value.code == 0
    assert captured.out == '{\n  "ok": true\n}\n'
    assert not captured.err
    assert request == {
        "host": None,
        "path": relative_url,
        "method": method,
        "socket_path": socket_path,
        "token_file": token_file,
    }


def test_status_summary_uses_bounded_control_plane_view(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    request: dict[str, object] = {}

    def fetch(host, path, *, method, socket_path, token_file):
        request.update(
            host=host,
            path=path,
            method=method,
            socket_path=socket_path,
            token_file=token_file,
        )
        return {"service": {"status": "ok"}, "queue": {}}

    monkeypatch.setattr("pynchy.__main__._fetch_control_payload", fetch)
    monkeypatch.setattr(sys, "argv", ["pynchy", "--host", "remote:8485", "status", "--summary"])

    with pytest.raises(SystemExit) as exited:
        cli.main()

    assert exited.value.code == 0
    assert capsys.readouterr().out == '{"queue":{},"service":{"status":"ok"}}\n'
    assert request == {
        "host": "remote:8485",
        "path": "/status?summary=1",
        "method": "GET",
        "socket_path": None,
        "token_file": None,
    }


def test_status_summary_reports_control_plane_failure(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    def fail(*args, **kwargs):
        raise OSError("unreachable")

    monkeypatch.setattr("pynchy.__main__._fetch_control_payload", fail)
    monkeypatch.setattr(sys, "argv", ["pynchy", "status", "--summary"])

    with pytest.raises(SystemExit) as exited:
        cli.main()

    captured = capsys.readouterr()
    assert exited.value.code == 1
    assert not captured.out
    assert captured.err == "Status summary failed: unreachable\n"


def test_ops_command_runs_the_fixed_operation(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str], tmp_path: Path
) -> None:
    settings = make_settings(project_root=tmp_path)
    operations: list[str] = []

    def run(config, operation: str) -> str:
        assert config is settings.ops
        operations.append(operation)
        return "bounded output"

    monkeypatch.setattr("pynchy.config.api.get_settings", lambda: settings)
    monkeypatch.setattr("pynchy.__main__.run_remote_op", run)
    monkeypatch.setattr(sys, "argv", ["pynchy", "ops", "messages"])

    with pytest.raises(SystemExit) as exited:
        cli.main()

    captured = capsys.readouterr()
    assert exited.value.code == 0
    assert captured.out == "bounded output\n"
    assert not captured.err
    assert operations == ["messages"]


def test_ops_command_reports_bounded_failure(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str], tmp_path: Path
) -> None:
    settings = make_settings(project_root=tmp_path)

    def fail(config, operation: str) -> str:
        raise OSError("SSH unavailable")

    monkeypatch.setattr("pynchy.config.api.get_settings", lambda: settings)
    monkeypatch.setattr("pynchy.__main__.run_remote_op", fail)
    monkeypatch.setattr(sys, "argv", ["pynchy", "ops", "logs"])

    with pytest.raises(SystemExit) as exited:
        cli.main()

    captured = capsys.readouterr()
    assert exited.value.code == 1
    assert not captured.out
    assert captured.err == "Ops logs failed: SSH unavailable\n"
