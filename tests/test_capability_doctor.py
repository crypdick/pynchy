"""Behavioral tests for the read-only ``pynchy doctor`` CLI."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from pynchy import __main__ as cli

if TYPE_CHECKING:
    from pathlib import Path


class _Response:
    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _payload() -> dict[str, object]:
    return {
        "workspace": "personal",
        "capabilities": [
            {
                "id": "chat.matrix.route.send",
                "status": "unavailable",
                "reason": "Matrix gateway binary is unavailable",
                "setup_hint": "Install the Matrix gateway.",
                "recovery_hint": "Check PYNCHY_MATRIX_GATEWAY.",
            }
        ],
    }


def _run_doctor(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    *arguments: str,
) -> tuple[int, str]:
    """Run the public CLI and return its exit code plus visible stdout."""
    monkeypatch.delenv("PYNCHY_CONTROL_TOKEN", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["pynchy", "--token-file", str(tmp_path / "missing-token"), *arguments],
    )

    with pytest.raises(SystemExit) as exited:
        cli.main()

    return exited.value.code, capsys.readouterr().out


def test_doctor_encodes_workspace_and_prints_raw_snapshot(monkeypatch, capsys, tmp_path: Path):
    opened = Mock(return_value=_Response(_payload()))
    monkeypatch.setattr(cli.urllib.request, "urlopen", opened)

    exit_code, output = _run_doctor(
        monkeypatch,
        capsys,
        tmp_path,
        "--host",
        "localhost:8484",
        "doctor",
        "--workspace",
        "personal chats",
        "--json",
    )

    assert exit_code == 0
    assert json.loads(output) == _payload()
    opened.assert_called_once_with(
        "http://localhost:8484/capabilities?workspace=personal+chats",
        timeout=10,
    )


def test_doctor_renders_status_reason_and_remediation(monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setattr(cli.urllib.request, "urlopen", Mock(return_value=_Response(_payload())))

    exit_code, output = _run_doctor(
        monkeypatch,
        capsys,
        tmp_path,
        "--host",
        "status.internal:9999",
        "doctor",
        "--workspace",
        "personal",
    )

    assert exit_code == 0
    assert "Capabilities for personal:" in output
    assert "[unavailable] chat.matrix.route.send - Matrix gateway binary is unavailable" in output
    assert "setup: Install the Matrix gateway." in output
    assert "recover: Check PYNCHY_MATRIX_GATEWAY." in output


def test_doctor_without_a_socket_uses_loopback_tcp(monkeypatch, capsys, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    opened = Mock(return_value=_Response(_payload()))
    monkeypatch.setattr(cli.urllib.request, "urlopen", opened)

    exit_code, _ = _run_doctor(monkeypatch, capsys, tmp_path, "doctor", "--json")

    assert exit_code == 0
    opened.assert_called_once_with("http://localhost:8484/capabilities", timeout=10)


def test_doctor_reports_an_invalid_capability_response(monkeypatch, capsys, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.urllib.request, "urlopen", Mock(return_value=_Response([])))

    monkeypatch.setattr(
        sys,
        "argv",
        ["pynchy", "--token-file", str(tmp_path / "missing-token"), "doctor"],
    )
    with pytest.raises(SystemExit) as exited:
        cli.main()

    assert exited.value.code == 1
    assert capsys.readouterr().err == (
        "Capability doctor failed: Capability endpoint returned a non-object response\n"
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"workspaces": {}}, "invalid workspace list"),
        ({"workspaces": ["personal"]}, "invalid workspace snapshot"),
        (
            {"workspaces": [{"workspace": "personal", "capabilities": {}}]},
            "invalid capability list",
        ),
        (
            {"workspaces": [{"workspace": "personal", "capabilities": ["broken"]}]},
            "invalid capability",
        ),
    ],
)
def test_doctor_rejects_malformed_workspace_capability_payload(
    monkeypatch,
    capsys,
    tmp_path: Path,
    payload: object,
    message: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.urllib.request, "urlopen", Mock(return_value=_Response(payload)))
    monkeypatch.setattr(
        sys,
        "argv",
        ["pynchy", "--token-file", str(tmp_path / "missing-token"), "doctor"],
    )

    with pytest.raises(SystemExit) as exited:
        cli.main()

    assert exited.value.code == 1
    assert (
        capsys.readouterr().err
        == f"Capability doctor failed: Capability endpoint returned an {message}\n"
    )
