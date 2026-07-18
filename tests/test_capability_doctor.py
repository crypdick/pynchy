"""Tests for the read-only capability doctor CLI."""

from __future__ import annotations

import json
import sys
from unittest.mock import Mock

import pytest

from pynchy import __main__ as cli


class _Response:
    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode()

    def __enter__(self):
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
                "id": "chat.matrix.message.send",
                "status": "unavailable",
                "reason": "Matrix gateway binary is unavailable",
                "setup_hint": "Install the Matrix gateway.",
                "recovery_hint": "Check PYNCHY_MATRIX_GATEWAY.",
            }
        ],
    }


def test_doctor_url_encodes_workspace_name():
    url = cli._doctor_url(  # allow: private-test-imports - pure CLI URL contract.
        "localhost:8484",
        "personal chats",
    )

    assert url == "http://localhost:8484/capabilities?workspace=personal+chats"


def test_doctor_renders_status_reason_and_remediation():
    output = (
        cli._render_capability_doctor(  # allow: private-test-imports - pure CLI renderer contract.
            _payload()
        )
    )

    assert "Capabilities for personal:" in output
    assert "[unavailable] chat.matrix.message.send" in output
    assert "setup: Install the Matrix gateway." in output
    assert "recover: Check PYNCHY_MATRIX_GATEWAY." in output


def test_doctor_fetches_json_snapshot(monkeypatch, capsys):
    opened = Mock(return_value=_Response(_payload()))
    monkeypatch.setattr(cli.urllib.request, "urlopen", opened)

    result = cli._doctor(  # allow: private-test-imports - command transport boundary.
        "localhost:8484",
        "personal",
        json_output=True,
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out) == _payload()
    opened.assert_called_once_with(
        "http://localhost:8484/capabilities?workspace=personal",
        timeout=10,
    )


def test_main_dispatches_doctor_options(monkeypatch):
    doctor = Mock(return_value=0)
    monkeypatch.setattr(cli, "_doctor", doctor)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pynchy",
            "--host",
            "status.internal:9999",
            "doctor",
            "--workspace",
            "personal",
            "--json",
        ],
    )

    with pytest.raises(SystemExit, match="0"):
        cli.main()

    doctor.assert_called_once_with(
        "status.internal:9999",
        "personal",
        json_output=True,
    )
