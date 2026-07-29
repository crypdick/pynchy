"""Filesystem contracts for host-to-container IPC delivery."""

from __future__ import annotations

import json

from pynchy.host.container_manager.ipc.write import (
    configure_ipc_base_dir,
    ipc_response_path,
    write_ipc_close_sentinel,
    write_ipc_message,
    write_ipc_response,
)


def test_message_delivery_writes_complete_envelope(tmp_path) -> None:
    configure_ipc_base_dir(tmp_path)

    write_ipc_message(
        "project",
        "Ship it.",
        turn_id="turn-1",
        query_id="query-1",
        metadata={"source": "host"},
    )

    messages = list((tmp_path / "project" / "input").glob("*.json"))
    assert len(messages) == 1
    assert json.loads(messages[0].read_text()) == {
        "type": "message",
        "text": "Ship it.",
        "turn_id": "turn-1",
        "query_id": "query-1",
        "metadata": {"source": "host"},
    }


def test_message_delivery_omits_absent_optional_fields(tmp_path) -> None:
    configure_ipc_base_dir(tmp_path)

    write_ipc_message("project", "Ship it.")

    message = next((tmp_path / "project" / "input").glob("*.json"))
    assert json.loads(message.read_text()) == {"type": "message", "text": "Ship it."}


def test_control_and_response_files_use_the_same_configured_root(tmp_path) -> None:
    configure_ipc_base_dir(tmp_path)

    write_ipc_close_sentinel("project")
    response_path = ipc_response_path("project", "request-1")
    write_ipc_response(response_path, {"result": {"ok": True}})

    assert not (tmp_path / "project" / "input" / "_close").read_text()
    assert response_path == tmp_path / "project" / "responses" / "request-1.json"
    assert json.loads(response_path.read_text()) == {"result": {"ok": True}}
