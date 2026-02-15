"""Tests for dependency adapters.

Tests critical routing and broadcasting logic in adapters.py:
- GroupRegistry.god_chat_jid() — finding the god group for notifications
- HostMessageBroadcaster — dual store+broadcast with correct formatting
- EventBusAdapter — event type conversion for SSE/TUI bridge
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

from pynchy.adapters import (
    EventBusAdapter,
    GroupMetadataManager,
    GroupRegistry,
    HostMessageBroadcaster,
    MessageBroadcaster,
    SessionManager,
)
from pynchy.event_bus import (
    AgentActivityEvent,
    AgentTraceEvent,
    ChatClearedEvent,
    EventBus,
    MessageEvent,
)
from pynchy.types import RegisteredGroup


def _group(*, name: str = "Test", folder: str = "test", is_god: bool = False) -> RegisteredGroup:
    return RegisteredGroup(
        name=name, folder=folder, trigger="@pynchy", added_at="2024-01-01", is_god=is_god
    )


class FakeChannel:
    """Minimal channel for adapter tests."""

    def __init__(self, *, connected: bool = True):
        self.name = "fake"
        self._connected = connected
        self.sent: list[tuple[str, str]] = []

    def is_connected(self) -> bool:
        return self._connected

    async def send_message(self, jid: str, text: str) -> None:
        self.sent.append((jid, text))


# ---------------------------------------------------------------------------
# GroupRegistry
# ---------------------------------------------------------------------------


class TestGroupRegistry:
    """Test god_chat_jid() which finds the god group for system notifications."""

    def test_finds_god_group_jid(self):
        groups = {
            "regular@g.us": _group(name="Regular"),
            "god@g.us": _group(name="God", is_god=True),
        }
        registry = GroupRegistry(groups)
        assert registry.god_chat_jid() == "god@g.us"

    def test_returns_empty_string_when_no_god_group(self):
        groups = {
            "a@g.us": _group(name="A"),
            "b@g.us": _group(name="B"),
        }
        registry = GroupRegistry(groups)
        assert registry.god_chat_jid() == ""

    def test_returns_empty_string_when_no_groups(self):
        registry = GroupRegistry({})
        assert registry.god_chat_jid() == ""

    def test_returns_first_god_group_if_multiple(self):
        """If somehow multiple god groups exist, return the first one found."""
        groups = {
            "god1@g.us": _group(name="God1", is_god=True),
            "god2@g.us": _group(name="God2", is_god=True),
        }
        registry = GroupRegistry(groups)
        result = registry.god_chat_jid()
        assert result in ("god1@g.us", "god2@g.us")

    def test_registered_groups_returns_reference(self):
        groups = {"a@g.us": _group(name="A")}
        registry = GroupRegistry(groups)
        assert registry.registered_groups() is groups


# ---------------------------------------------------------------------------
# HostMessageBroadcaster
# ---------------------------------------------------------------------------


class TestHostMessageBroadcaster:
    """Test broadcast_host_message and broadcast_system_notice.

    These are the critical paths for operational notifications and system
    announcements. They must store to DB, send to channels, and emit events.
    """

    def _make_broadcaster(self) -> tuple[HostMessageBroadcaster, FakeChannel, AsyncMock, list]:
        channel = FakeChannel()
        msg_broadcaster = MessageBroadcaster([channel])
        store_fn = AsyncMock()
        emitted: list[Any] = []
        host_broadcaster = HostMessageBroadcaster(msg_broadcaster, store_fn, emitted.append)
        return host_broadcaster, channel, store_fn, emitted

    async def test_host_message_stores_in_db(self):
        broadcaster, _, store_fn, _ = self._make_broadcaster()
        await broadcaster.broadcast_host_message("group@g.us", "⚠️ Error occurred")

        store_fn.assert_called_once()
        kwargs = store_fn.call_args.kwargs
        assert kwargs["chat_jid"] == "group@g.us"
        assert kwargs["sender"] == "host"
        assert kwargs["sender_name"] == "host"
        assert kwargs["content"] == "⚠️ Error occurred"
        assert kwargs["is_from_me"] is True

    async def test_host_message_sends_to_channel_with_emoji_prefix(self):
        broadcaster, channel, _, _ = self._make_broadcaster()
        await broadcaster.broadcast_host_message("group@g.us", "Test message")

        assert len(channel.sent) == 1
        jid, text = channel.sent[0]
        assert jid == "group@g.us"
        assert text.startswith("\U0001f3e0")  # 🏠 emoji prefix
        assert "Test message" in text

    async def test_host_message_emits_event(self):
        broadcaster, _, _, emitted = self._make_broadcaster()
        await broadcaster.broadcast_host_message("group@g.us", "Test")

        assert len(emitted) == 1
        event = emitted[0]
        assert isinstance(event, MessageEvent)
        assert event.chat_jid == "group@g.us"
        assert event.sender_name == "host"
        assert event.content == "Test"
        assert event.is_bot is True

    async def test_system_notice_stores_with_system_notice_sender(self):
        broadcaster, _, store_fn, _ = self._make_broadcaster()
        await broadcaster.broadcast_system_notice("group@g.us", "Config changed")

        kwargs = store_fn.call_args.kwargs
        assert kwargs["sender"] == "system_notice"
        assert kwargs["sender_name"] == "system_notice"

    async def test_system_notice_sends_to_channel_with_megaphone_prefix(self):
        broadcaster, channel, _, _ = self._make_broadcaster()
        await broadcaster.broadcast_system_notice("group@g.us", "Update")

        assert len(channel.sent) == 1
        _, text = channel.sent[0]
        assert text.startswith("\U0001f4e2")  # 📢 emoji prefix

    async def test_host_message_id_starts_with_host_prefix(self):
        broadcaster, _, store_fn, _ = self._make_broadcaster()
        await broadcaster.broadcast_host_message("group@g.us", "Test")

        msg_id = store_fn.call_args.kwargs["id"]
        assert msg_id.startswith("host-")

    async def test_system_notice_id_starts_with_sys_notice_prefix(self):
        broadcaster, _, store_fn, _ = self._make_broadcaster()
        await broadcaster.broadcast_system_notice("group@g.us", "Test")

        msg_id = store_fn.call_args.kwargs["id"]
        assert msg_id.startswith("sys-notice-")


# ---------------------------------------------------------------------------
# MessageBroadcaster
# ---------------------------------------------------------------------------


class TestMessageBroadcaster:
    """Test channel broadcast behavior including error suppression."""

    async def test_sends_to_all_connected_channels(self):
        ch1 = FakeChannel()
        ch2 = FakeChannel()
        broadcaster = MessageBroadcaster([ch1, ch2])
        await broadcaster._broadcast_to_channels("group@g.us", "hello")

        assert len(ch1.sent) == 1
        assert len(ch2.sent) == 1

    async def test_skips_disconnected_channels(self):
        connected = FakeChannel(connected=True)
        disconnected = FakeChannel(connected=False)
        broadcaster = MessageBroadcaster([connected, disconnected])
        await broadcaster._broadcast_to_channels("group@g.us", "hello")

        assert len(connected.sent) == 1
        assert len(disconnected.sent) == 0

    async def test_suppresses_channel_errors(self):
        """Channel send failures should be silently suppressed."""

        class FailingChannel(FakeChannel):
            async def send_message(self, jid: str, text: str) -> None:
                raise ConnectionError("channel down")

        failing = FailingChannel()
        working = FakeChannel()
        broadcaster = MessageBroadcaster([failing, working])

        # Should not raise
        await broadcaster._broadcast_to_channels("group@g.us", "hello")
        assert len(working.sent) == 1

    async def test_broadcast_formatted_applies_format(self):
        """_broadcast_formatted applies per-channel formatting."""
        ch = FakeChannel()
        broadcaster = MessageBroadcaster([ch])

        # format_outbound strips internal tags and may adjust text
        from unittest.mock import patch as _patch

        with _patch("pynchy.adapters.format_outbound", return_value="formatted text"):
            await broadcaster._broadcast_formatted("group@g.us", "raw text")

        assert len(ch.sent) == 1
        assert ch.sent[0][1] == "formatted text"

    async def test_broadcast_formatted_skips_when_formatter_returns_empty(self):
        """_broadcast_formatted skips send when format_outbound returns empty string."""
        ch = FakeChannel()
        broadcaster = MessageBroadcaster([ch])

        from unittest.mock import patch as _patch

        with _patch("pynchy.adapters.format_outbound", return_value=""):
            await broadcaster._broadcast_formatted("group@g.us", "raw text")

        assert len(ch.sent) == 0

    async def test_broadcast_formatted_suppresses_channel_errors(self):
        """_broadcast_formatted suppresses channel send errors like _broadcast_to_channels."""

        class FailingChannel(FakeChannel):
            async def send_message(self, jid: str, text: str) -> None:
                raise OSError("send failed")

        failing = FailingChannel()
        working = FakeChannel()
        broadcaster = MessageBroadcaster([failing, working])

        from unittest.mock import patch as _patch

        with _patch("pynchy.adapters.format_outbound", return_value="ok"):
            await broadcaster._broadcast_formatted("group@g.us", "raw")

        assert len(working.sent) == 1

    async def test_broadcast_to_empty_channel_list(self):
        """Broadcasting to empty channel list is a no-op."""
        broadcaster = MessageBroadcaster([])
        # Should not raise
        await broadcaster._broadcast_to_channels("group@g.us", "hello")


# ---------------------------------------------------------------------------
# EventBusAdapter
# ---------------------------------------------------------------------------


class TestEventBusAdapter:
    """Test event type conversion from typed events to callback dicts.

    The EventBusAdapter bridges internal typed events to the HTTP/SSE API.
    Wrong conversion means the TUI shows stale or incorrect data.
    """

    async def test_converts_message_event(self):
        bus = EventBus()
        adapter = EventBusAdapter(bus)
        received: list[dict] = []

        adapter.subscribe_events(lambda d: asyncio.coroutine(lambda: received.append(d))())

        # Subscribe and emit
        async def callback(data: dict) -> None:
            received.append(data)

        adapter.subscribe_events(callback)
        bus.emit(
            MessageEvent(
                chat_jid="group@g.us",
                sender_name="Alice",
                content="hello",
                timestamp="2024-01-01T00:00:00Z",
                is_bot=False,
            )
        )
        await asyncio.sleep(0.05)

        msg_events = [e for e in received if e.get("type") == "message"]
        assert len(msg_events) >= 1
        event = msg_events[0]
        assert event["chat_jid"] == "group@g.us"
        assert event["sender_name"] == "Alice"
        assert event["content"] == "hello"
        assert event["is_bot"] is False

    async def test_converts_agent_activity_event(self):
        bus = EventBus()
        adapter = EventBusAdapter(bus)
        received: list[dict] = []

        async def callback(data: dict) -> None:
            received.append(data)

        adapter.subscribe_events(callback)
        bus.emit(AgentActivityEvent(chat_jid="group@g.us", active=True))
        await asyncio.sleep(0.05)

        activity_events = [e for e in received if e.get("type") == "agent_activity"]
        assert len(activity_events) == 1
        assert activity_events[0]["active"] is True

    async def test_converts_agent_trace_event(self):
        bus = EventBus()
        adapter = EventBusAdapter(bus)
        received: list[dict] = []

        async def callback(data: dict) -> None:
            received.append(data)

        adapter.subscribe_events(callback)
        bus.emit(
            AgentTraceEvent(
                chat_jid="group@g.us",
                trace_type="tool_use",
                data={"tool_name": "Bash", "tool_input": {"command": "ls"}},
            )
        )
        await asyncio.sleep(0.05)

        trace_events = [e for e in received if e.get("type") == "agent_trace"]
        assert len(trace_events) == 1
        assert trace_events[0]["trace_type"] == "tool_use"
        # Data fields are spread into the event dict
        assert trace_events[0]["tool_name"] == "Bash"

    async def test_converts_chat_cleared_event(self):
        bus = EventBus()
        adapter = EventBusAdapter(bus)
        received: list[dict] = []

        async def callback(data: dict) -> None:
            received.append(data)

        adapter.subscribe_events(callback)
        bus.emit(ChatClearedEvent(chat_jid="group@g.us"))
        await asyncio.sleep(0.05)

        clear_events = [e for e in received if e.get("type") == "chat_cleared"]
        assert len(clear_events) == 1
        assert clear_events[0]["chat_jid"] == "group@g.us"

    async def test_unsubscribe_stops_receiving_events(self):
        bus = EventBus()
        adapter = EventBusAdapter(bus)
        received: list[dict] = []

        async def callback(data: dict) -> None:
            received.append(data)

        unsub = adapter.subscribe_events(callback)
        unsub()

        bus.emit(
            MessageEvent(
                chat_jid="group@g.us",
                sender_name="Alice",
                content="hello",
                timestamp="2024-01-01T00:00:00Z",
                is_bot=False,
            )
        )
        await asyncio.sleep(0.05)

        assert len(received) == 0


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


class TestSessionManager:
    """Test session state management."""

    async def test_clear_session_removes_from_dict(self):
        sessions = {"test-group": "session-123"}
        cleared: set[str] = set()
        manager = SessionManager(sessions, cleared)

        from unittest.mock import patch as _patch

        with _patch("pynchy.adapters.clear_session", new_callable=AsyncMock):
            await manager.clear_session("test-group")

        assert "test-group" not in sessions
        assert "test-group" in cleared

    async def test_clear_session_is_idempotent(self):
        sessions: dict[str, str] = {}
        cleared: set[str] = set()
        manager = SessionManager(sessions, cleared)

        from unittest.mock import patch as _patch

        with _patch("pynchy.adapters.clear_session", new_callable=AsyncMock):
            # Clearing a non-existent session should not raise
            await manager.clear_session("nonexistent")

        assert "nonexistent" in cleared


# ---------------------------------------------------------------------------
# GroupMetadataManager
# ---------------------------------------------------------------------------


class TestGroupMetadataManager:
    """Test group metadata queries."""

    def test_get_groups_returns_registered_groups(self):
        groups = {
            "a@g.us": _group(name="Alpha", folder="alpha"),
            "b@g.us": _group(name="Beta", folder="beta"),
        }
        manager = GroupMetadataManager(groups, [], AsyncMock())
        result = manager.get_groups()

        assert len(result) == 2
        names = {g["name"] for g in result}
        assert names == {"Alpha", "Beta"}

    def test_channels_connected_returns_true_when_any_connected(self):
        connected = FakeChannel(connected=True)
        disconnected = FakeChannel(connected=False)
        manager = GroupMetadataManager({}, [connected, disconnected], AsyncMock())
        assert manager.channels_connected() is True

    def test_channels_connected_returns_false_when_all_disconnected(self):
        ch1 = FakeChannel(connected=False)
        ch2 = FakeChannel(connected=False)
        manager = GroupMetadataManager({}, [ch1, ch2], AsyncMock())
        assert manager.channels_connected() is False

    def test_channels_connected_returns_false_when_no_channels(self):
        manager = GroupMetadataManager({}, [], AsyncMock())
        assert manager.channels_connected() is False
