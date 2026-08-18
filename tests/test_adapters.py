"""Tests for dependency adapters.

Tests critical routing and broadcasting logic in adapters.py:
- resolve_admin_notification_jid() — finding the configured notification target
- HostMessageBroadcaster — dual store+broadcast with correct formatting
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from conftest import NullChannel

from pynchy.event_bus import MessageEvent
from pynchy.host.orchestrator.adapters import (
    GroupMetadataManager,
    HostMessageBroadcaster,
    MessageBroadcaster,
    SessionManager,
    resolve_admin_notification_jid,
)
from pynchy.plugins.api import (
    OutboundEvent,
    OutboundEventType,
)
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


# ---------------------------------------------------------------------------
# HostMessageBroadcaster
# ---------------------------------------------------------------------------


class TestHostMessageBroadcaster:
    """Test broadcast_host_message and broadcast_system_notice.

    These are the critical paths for operational notifications and system
    announcements. They must store to DB, send to channels, and emit events.
    """

    def _make_broadcaster(
        self,
    ) -> tuple[HostMessageBroadcaster, FakeChannel, AsyncMock, AsyncMock, list]:
        channel = FakeChannel()
        msg_broadcaster = MessageBroadcaster([channel])
        store_host_fn = AsyncMock()
        store_notice_fn = AsyncMock()
        emitted: list[Any] = []
        host_broadcaster = HostMessageBroadcaster(
            msg_broadcaster, store_host_fn, store_notice_fn, emitted.append
        )
        return host_broadcaster, channel, store_host_fn, store_notice_fn, emitted

    async def test_host_message_stores_in_db(self):
        broadcaster, _, store_host_fn, _, _ = self._make_broadcaster()
        await broadcaster.broadcast_host_message("group@g.us", "⚠️ Error occurred")

        store_host_fn.assert_called_once()
        kwargs = store_host_fn.call_args.kwargs
        assert kwargs["chat_jid"] == "group@g.us"
        assert kwargs["sender"] == "host"
        assert kwargs["sender_name"] == "host"
        assert kwargs["content"] == "⚠️ Error occurred"
        assert kwargs["is_from_me"] is True

    async def test_host_message_sends_event_to_channel(self):
        broadcaster, channel, _, _, _ = self._make_broadcaster()
        await broadcaster.broadcast_host_message("group@g.us", "Test message")

        assert len(channel.sent) == 1
        jid, event = channel.sent[0]
        assert jid == "group@g.us"
        assert event.type == OutboundEventType.HOST
        assert event.content == "Test message"

    async def test_host_message_emits_event(self):
        broadcaster, _, _, _, emitted = self._make_broadcaster()
        await broadcaster.broadcast_host_message("group@g.us", "Test")

        assert len(emitted) == 1
        event = emitted[0]
        assert isinstance(event, MessageEvent)
        assert event.chat_jid == "group@g.us"
        assert event.sender_name == "host"
        assert event.content == "Test"
        assert event.is_bot is True

    async def test_system_notice_stores_with_system_notice_sender(self):
        broadcaster, _, _, store_notice_fn, _ = self._make_broadcaster()
        await broadcaster.broadcast_system_notice("group@g.us", "Config changed")

        store_notice_fn.assert_called_once()
        kwargs = store_notice_fn.call_args.kwargs
        assert kwargs["sender"] == "system_notice"
        assert kwargs["sender_name"] == "System"

    async def test_system_notice_prefixes_content(self):
        """System notices are prefixed with [System Notice] for LLM visibility."""
        broadcaster, _, _, store_notice_fn, _ = self._make_broadcaster()
        await broadcaster.broadcast_system_notice("group@g.us", "Config changed")

        kwargs = store_notice_fn.call_args.kwargs
        assert kwargs["content"] == "[System Notice] Config changed"

    async def test_system_notice_uses_notice_store_fn(self):
        """System notices use the notice store fn, not the host store fn."""
        broadcaster, _, store_host_fn, store_notice_fn, _ = self._make_broadcaster()
        await broadcaster.broadcast_system_notice("group@g.us", "Update")

        store_host_fn.assert_not_called()
        store_notice_fn.assert_called_once()

    async def test_host_message_uses_host_store_fn(self):
        """Host messages use the host store fn, not the notice store fn."""
        broadcaster, _, store_host_fn, store_notice_fn, _ = self._make_broadcaster()
        await broadcaster.broadcast_host_message("group@g.us", "Status update")

        store_host_fn.assert_called_once()
        store_notice_fn.assert_not_called()

    async def test_system_notice_sends_event_to_channel(self):
        broadcaster, channel, _, _, _ = self._make_broadcaster()
        await broadcaster.broadcast_system_notice("group@g.us", "Update")

        assert len(channel.sent) == 1
        _, event = channel.sent[0]
        assert event.type == OutboundEventType.SYSTEM
        assert event.content == "[System Notice] Update"

    async def test_host_message_id_starts_with_host_prefix(self):
        broadcaster, _, store_host_fn, _, _ = self._make_broadcaster()
        await broadcaster.broadcast_host_message("group@g.us", "Test")

        msg_id = store_host_fn.call_args.kwargs["message_id"]
        assert msg_id.startswith("host-")

    async def test_system_notice_id_starts_with_sys_notice_prefix(self):
        broadcaster, _, _, store_notice_fn, _ = self._make_broadcaster()
        await broadcaster.broadcast_system_notice("group@g.us", "Test")

        msg_id = store_notice_fn.call_args.kwargs["message_id"]
        assert msg_id.startswith("sys-notice-")


# ---------------------------------------------------------------------------
# MessageBroadcaster
# ---------------------------------------------------------------------------


class TestMessageBroadcaster:
    """Test channel broadcast behavior including error suppression."""

    async def test_synthetic_user_input_reuses_channel_broadcast(self):
        channel = FakeChannel()
        broadcaster = MessageBroadcaster([channel])

        await broadcaster.broadcast_synthetic_user_input(
            "discord:channel:1", "use native search_skills"
        )

        jid, event = channel.sent[0]
        assert jid == "discord:channel:1"
        assert event.type is OutboundEventType.TEXT
        assert event.content == "use native search_skills"
        assert event.metadata == {"synthetic_user_input": True}

    async def test_sends_to_all_connected_channels(self):
        ch1 = FakeChannel()
        ch2 = FakeChannel()
        broadcaster = MessageBroadcaster([ch1, ch2])
        event = _make_event("hello")
        await broadcaster.broadcast_to_channels("group@g.us", event)

        assert len(ch1.sent) == 1
        assert len(ch2.sent) == 1

    async def test_skips_disconnected_channels(self):
        connected = FakeChannel(connected=True)
        disconnected = FakeChannel(connected=False)
        broadcaster = MessageBroadcaster([connected, disconnected])
        await broadcaster.broadcast_to_channels("group@g.us", _make_event("hello"))

        assert len(connected.sent) == 1
        assert len(disconnected.sent) == 0

    async def test_suppresses_channel_errors(self):
        """Channel send failures should be silently suppressed."""

        class FailingChannel(FakeChannel):
            async def send_event(self, jid: str, event: OutboundEvent) -> None:
                raise ConnectionError(CHANNEL_DOWN_MESSAGE)

        failing = FailingChannel()
        working = FakeChannel()
        broadcaster = MessageBroadcaster([failing, working])

        # Should not raise
        await broadcaster.broadcast_to_channels("group@g.us", _make_event("hello"))
        assert len(working.sent) == 1

    async def test_broadcast_to_empty_channel_list(self):
        """Broadcasting to empty channel list is a no-op."""
        broadcaster = MessageBroadcaster([])
        # Should not raise
        await broadcaster.broadcast_to_channels("group@g.us", _make_event("hello"))


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


class TestSessionManager:
    """Test session state management."""

    async def test_clear_session_removes_from_dict(self):
        sessions = {"test-group": "session-123"}
        cleared: set[str] = set()
        manager = SessionManager(sessions, cleared)

        with patch("pynchy.host.orchestrator.adapters.clear_session", new_callable=AsyncMock):
            await manager.clear_session("test-group")

        assert "test-group" not in sessions
        assert "test-group" in cleared

    async def test_clear_session_is_idempotent(self):
        sessions: dict[str, str] = {}
        cleared: set[str] = set()
        manager = SessionManager(sessions, cleared)

        with patch("pynchy.host.orchestrator.adapters.clear_session", new_callable=AsyncMock):
            # Clearing a non-existent session should not raise
            await manager.clear_session("nonexistent")

        assert "nonexistent" in cleared

    def test_active_sessions_excludes_cleared_and_unregistered_groups(self):
        manager = SessionManager(
            {"active": "session-1", "cleared": "session-2", "unknown": "session-3"},
            {"cleared"},
        )
        groups = {"chat:active": _group(folder="active")}

        assert manager.get_active_sessions(groups) == {"chat:active": "session-1"}


class TestGroupMetadataManager:
    async def test_get_available_groups_delegates(self):
        get_groups = AsyncMock(return_value=[{"jid": "chat:1"}])

        manager = GroupMetadataManager([], get_groups)

        assert await manager.get_available_groups() == [{"jid": "chat:1"}]

    async def test_sync_group_metadata_calls_supported_channels(self):
        class SyncingChannel(FakeChannel):
            def __init__(self) -> None:
                super().__init__()
                self.force: bool | None = None

            async def sync_group_metadata(self, *, force: bool) -> None:
                self.force = force

        syncing = SyncingChannel()
        manager = GroupMetadataManager([syncing, FakeChannel()], list)

        await manager.sync_group_metadata(force=True)

        assert syncing.force is True
