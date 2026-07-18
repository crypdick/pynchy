from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

from textual.widgets import Static

from pynchy.plugins.channels.tui import client as tui_client
from pynchy.plugins.channels.tui.client import ChatLog, PynchyTUI

if TYPE_CHECKING:
    from pathlib import Path

TEST_BEARER_TOKEN = "synthetic-test-token"  # noqa: S105, RUF100 - non-secret test fixture.


def _make_app():
    app = PynchyTUI("http://example.test")
    app._active_jid = "group@g.us"
    app._groups = [{"jid": "group@g.us", "name": "Test Group"}]

    chat_log = Mock(spec=ChatLog)
    header = Mock(spec=Static)

    def fake_query_one(selector, *_args):
        if selector == ChatLog:
            return chat_log
        if selector == "#chat-header":
            return header
        message = f"Unexpected selector: {selector!r}"
        raise AssertionError(message)

    app.query_one = fake_query_one  # type: ignore[method-assign]
    return app, chat_log, header


def test_handles_active_message_event(monkeypatch) -> None:
    app, _chat_log, _header = _make_app()
    render_message = Mock()
    monkeypatch.setattr(tui_client, "_render_message", render_message)

    app._handle_sse_event(
        {
            "type": "message",
            "chat_jid": "group@g.us",
            "sender_name": "Alice",
            "content": "hello",
            "timestamp": "2024-01-01T00:00:00Z",
        }
    )

    render_message.assert_called_once()


def test_skips_local_echo_message_event(monkeypatch) -> None:
    app, _chat_log, _header = _make_app()
    render_message = Mock()
    monkeypatch.setattr(tui_client, "_render_message", render_message)

    app._handle_sse_event(
        {
            "type": "message",
            "chat_jid": "group@g.us",
            "sender_name": "You",
            "content": "hello",
            "timestamp": "2024-01-01T00:00:00Z",
            "is_bot": False,
        }
    )

    render_message.assert_not_called()


def test_ignores_malformed_message_event(monkeypatch) -> None:
    app, _chat_log, _header = _make_app()
    render_message = Mock()
    monkeypatch.setattr(tui_client, "_render_message", render_message)

    app._handle_sse_event(
        {
            "type": "message",
            "chat_jid": "group@g.us",
            "content": "hello",
        }
    )

    render_message.assert_not_called()


def test_updates_header_for_active_agent_activity() -> None:
    app, _chat_log, header = _make_app()

    app._handle_sse_event(
        {
            "type": "agent_activity",
            "chat_jid": "group@g.us",
            "active": True,
        }
    )

    header.update.assert_called_once_with("Chat: Test Group [dim][thinking...][/dim]")


def test_run_tui_passes_local_socket_and_bearer_to_client(monkeypatch, tmp_path: Path) -> None:
    tui = Mock()
    tui_type = Mock(return_value=tui)
    monkeypatch.setattr(tui_client, "PynchyTUI", tui_type)
    socket_path = tmp_path / "pynchy.sock"

    tui_client.run_tui(
        None,
        socket_path=socket_path,
        bearer_token=TEST_BEARER_TOKEN,
    )

    tui_type.assert_called_once_with(
        base_url="http://localhost",
        unix_socket=socket_path,
        bearer_token=TEST_BEARER_TOKEN,
    )
    tui.run.assert_called_once_with()
