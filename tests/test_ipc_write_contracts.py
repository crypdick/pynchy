"""Filesystem contracts for host-to-container IPC delivery."""

from __future__ import annotations

import json

import pytest

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


def test_message_delivery_requires_ipc_root(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("pynchy.host.container_manager.ipc.write._ipc_base_dir", None)

    with pytest.raises(RuntimeError, match="IPC base directory has not been configured"):
        write_ipc_message("project", "Ship it.")


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


def test_close_sentinel_replaces_symlink_without_following_it(tmp_path) -> None:
    configure_ipc_base_dir(tmp_path)
    input_dir = tmp_path / "project" / "input"
    input_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    sentinel = input_dir / "_close"
    sentinel.symlink_to(outside)

    write_ipc_close_sentinel("project")

    assert outside.read_text() == "untouched"
    assert not sentinel.is_symlink()
    assert not sentinel.read_text()
