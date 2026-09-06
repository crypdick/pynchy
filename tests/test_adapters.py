"""Tests for dependency adapters.

Tests critical routing and broadcasting logic in adapters.py:
- resolve_admin_notification_jid() — finding the configured notification target
- Host notifications — matching durable history, channel output, and events
"""

from __future__ import annotations

import pytest
from conftest import NullChannel
from freezegun import freeze_time

from pynchy.agent_protocol.api import CheckpointControlState, InFlightTurn, InFlightWorkKind
from pynchy.event_bus import MessageEvent
from pynchy.host.orchestrator.adapters import (
    get_active_sessions,
    resolve_admin_notification_jid,
)
from pynchy.host.orchestrator.app import PynchyApp
from pynchy.host.orchestrator.dep_factory import make_http_deps
from pynchy.plugins.api import (
    OutboundEvent,
    OutboundEventType,
)
from pynchy.state.api import begin_in_flight_turn, get_chat_history, init_test_database, pause_chat
from pynchy.workspace.api import WorkspaceProfile

CHANNEL_DOWN_MESSAGE = "channel down"


def _make_event(content: str = "hello") -> OutboundEvent:
    return OutboundEvent(type=OutboundEventType.HOST, content=content)


def _group(
    *, jid: str = "test@g.us", name: str = "Test", folder: str = "test", is_admin: bool = False
) -> WorkspaceProfile:
    return WorkspaceProfile(
        jid=jid,
        name=name,
        folder=folder,
        trigger="@pynchy",
        added_at="2024-01-01",
        is_admin=is_admin,
    )


class FakeChannel(NullChannel):
    """Minimal channel for adapter tests."""

    def __init__(self, *, connected: bool = True):
        self.name = "fake"
        self._connected = connected
        self.sent: list[tuple[str, OutboundEvent]] = []

    def is_connected(self) -> bool:
        return self._connected

    def owns_jid(self, jid: str) -> bool:
        return True

    async def send_event(self, jid: str, event: OutboundEvent) -> None:
        self.sent.append((jid, event))


# ---------------------------------------------------------------------------
# resolve_admin_notification_jid
# ---------------------------------------------------------------------------


class TestResolveAdminNotificationJid:
    def test_prefers_configured_admin_workspace(self):
        groups = {
            "slack:legacy": _group(
                jid="slack:legacy", name="Legacy", folder="legacy", is_admin=True
            ),
            "discord:channel:admin": _group(
                jid="discord:channel:admin", name="Admin", folder="discord-admin", is_admin=True
            ),
        }

        assert resolve_admin_notification_jid(groups, "discord-admin") == "discord:channel:admin"

    def test_rejects_missing_configuration_without_fallback(self):
        groups = {"slack:legacy": _group(jid="slack:legacy", is_admin=True)}

        assert not resolve_admin_notification_jid(groups, None)

    def test_rejects_missing_configured_workspace_without_fallback(self):
        groups = {"slack:legacy": _group(jid="slack:legacy", is_admin=True)}

        assert not resolve_admin_notification_jid(groups, "discord-admin")

    def test_rejects_non_admin_configured_workspace(self):
        groups = {
            "discord:channel:general": _group(
                jid="discord:channel:general", folder="general", is_admin=False
            )
        }

        assert not resolve_admin_notification_jid(groups, "general")


@pytest.fixture
async def app() -> PynchyApp:
    await init_test_database()
    return PynchyApp()


@pytest.mark.parametrize(
    ("method", "sender", "sender_name", "message_type", "event_type", "content", "prefix"),
    [
        (
            "broadcast_host_message",
            "host",
            "host",
            "host",
            OutboundEventType.HOST,
            "Update",
            "host-",
        ),
        (
            "broadcast_system_notice",
            "system_notice",
            "System",
            "user",
            OutboundEventType.SYSTEM,
            "[System Notice] Update",
            "sys-notice-",
        ),
    ],
)
async def test_host_notification_history_matches_channel_and_event(
    app,
    monkeypatch,
    method,
    sender,
    sender_name,
    message_type,
    event_type,
    content,
    prefix,
):
    channel = FakeChannel()
    app.channels = [channel]
    emitted = []
    monkeypatch.setattr(app.event_bus, "emit", emitted.append)

    await getattr(app, method)("group@g.us", "Update")

    [stored] = await get_chat_history("group@g.us", limit=10)
    assert stored.id.startswith(prefix)
    assert stored.sender == sender
    assert stored.sender_name == sender_name
    assert stored.message_type == message_type
    assert stored.content == content
    assert stored.is_from_me is True
    assert stored.metadata == {"source": "host_broadcaster"}
    assert channel.sent == [("group@g.us", OutboundEvent(type=event_type, content=content))]
    assert emitted == [MessageEvent("group@g.us", sender_name, content, stored.timestamp, True)]


@pytest.mark.parametrize(
    "pause_source",
    ["durable", CheckpointControlState.PAUSE_REQUESTED, CheckpointControlState.PAUSED],
)
async def test_paused_chat_keeps_host_confirmation_but_suppresses_notice(app, pause_source):
    channel = FakeChannel()
    app.channels = [channel]
    if pause_source == "durable":
        await pause_chat("group@g.us")
    else:
        await begin_in_flight_turn(
            InFlightTurn(
                turn_id="turn-1",
                chat_jid="group@g.us",
                group_folder="test",
                work_kind=InFlightWorkKind.INTERACTIVE,
                input_messages=[],
                input_start_cursor="",
                input_end_cursor="",
                started_at="2026-09-05T12:00:00Z",
                control_state=pause_source,
            )
        )

    await app.broadcast_system_notice("group@g.us", "Config changed")
    await app.broadcast_host_message("group@g.us", "Paused")

    history = await get_chat_history("group@g.us", limit=10)
    assert [(message.message_type, message.content) for message in history] == [("host", "Paused")]
    assert [event.content for _, event in channel.sent] == ["Paused"]


async def test_host_notifications_follow_channel_changes(app):
    first, second = FakeChannel(), FakeChannel()
    app.channels = [first]
    await app.broadcast_host_message("group@g.us", "First")
    app.channels = [second]
    await app.broadcast_system_notice("group@g.us", "Second")
    assert [event.content for _, event in first.sent] == ["First"]
    assert [event.content for _, event in second.sent] == ["[System Notice] Second"]


# ---------------------------------------------------------------------------
# Raw channel broadcasts
# ---------------------------------------------------------------------------


class TestChannelBroadcast:
    """Test channel broadcast behavior including error suppression."""

    async def test_synthetic_user_input_reuses_channel_broadcast(self, app):
        channel = FakeChannel()
        app.channels = [channel]

        await make_http_deps(app).broadcast_synthetic_user_input(
            "discord:channel:1", "use native search_skills"
        )

        jid, event = channel.sent[0]
        assert jid == "discord:channel:1"
        assert event.type is OutboundEventType.TEXT
        assert event.content == "use native search_skills"
        assert event.metadata == {"synthetic_user_input": True}

    async def test_sends_to_all_connected_channels(self, app):
        ch1 = FakeChannel()
        ch2 = FakeChannel()
        app.channels = [ch1, ch2]
        event = _make_event("hello")
        await app.broadcast_to_channels("group@g.us", event)

        assert len(ch1.sent) == 1
        assert len(ch2.sent) == 1

    async def test_skips_disconnected_channels(self, app):
        connected = FakeChannel(connected=True)
        disconnected = FakeChannel(connected=False)
        app.channels = [connected, disconnected]
        await app.broadcast_to_channels("group@g.us", _make_event("hello"))

        assert len(connected.sent) == 1
        assert len(disconnected.sent) == 0

    async def test_suppresses_channel_errors(self, app):
        """Channel send failures should be silently suppressed."""

        class FailingChannel(FakeChannel):
            async def send_event(self, jid: str, event: OutboundEvent) -> None:
                raise ConnectionError(CHANNEL_DOWN_MESSAGE)

        failing = FailingChannel()
        working = FakeChannel()
        app.channels = [failing, working]

        # Should not raise
        await app.broadcast_to_channels("group@g.us", _make_event("hello"))
        assert len(working.sent) == 1

    async def test_broadcast_to_empty_channel_list(self, app):
        """Broadcasting to empty channel list is a no-op."""
        app.channels = []
        # Should not raise
        await app.broadcast_to_channels("group@g.us", _make_event("hello"))


def test_active_sessions_filters_cleared_unregistered_and_empty_bindings():
    sessions = {
        "active": "session-1",
        "cleared": "session-2",
        "unknown": "session-3",
        "empty": "",
        "anonymous": "session-4",
    }
    groups = {
        "chat:alias": _group(folder="active"),
        "chat:active": _group(folder="active"),
        "chat:cleared": _group(folder="cleared"),
        "chat:empty": _group(folder="empty"),
        "": _group(folder="anonymous"),
    }

    assert get_active_sessions(sessions, {"cleared"}, groups) == {"chat:active": "session-1"}


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("broadcast_host_message", ["First", "Second"]),
        ("broadcast_system_notice", ["[System Notice] First", "[System Notice] Second"]),
    ],
)
async def test_host_notifications_keep_messages_sent_in_the_same_millisecond(method, expected):
    await init_test_database()
    app = PynchyApp()
    with freeze_time("2026-09-05T12:00:00Z", real_asyncio=True):
        send = getattr(app, method)
        await send("group@g.us", "First")
        await send("group@g.us", "Second")

    history = await get_chat_history("group@g.us", limit=10)
    assert [message.content for message in history] == expected
    assert len({message.id for message in history}) == 2
