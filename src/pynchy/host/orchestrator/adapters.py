"""Dependency adapters for subsystem integration.

Provides concrete implementations of Protocol interfaces used by task scheduler,
HTTP server, and IPC watcher. Reduces boilerplate delegation code in PynchyApp.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pynchy.event_bus import (  # noqa: TC001, RUF100 - beartype resolves adapter annotations at runtime.
    AgentActivityEvent,
    AgentTraceEvent,
    ChatClearedEvent,
    EventBus,
    MessageEvent,
)
from pynchy.host.orchestrator.messaging.sender import broadcast
from pynchy.state import clear_session, get_all_tasks, get_chat_history
from pynchy.types import (
    Channel,
    GroupFolder,
    NewMessage,
    OutboundEvent,
    OutboundEventType,
    WorkspaceProfile,
)
from pynchy.utils import create_background_task, generate_message_id

# Type aliases for callback signatures used across adapters
StoreMessageFn = Callable[..., Awaitable[None]]
EmitEventFn = Callable[..., None]


class MessageBroadcaster:
    """Broadcasts messages to all connected channels.

    Satisfies the ``BusDeps`` protocol from ``messaging.bus`` so that
    ``broadcast_to_channels`` delegates to the single ``bus.broadcast()``
    code path (JID resolution, ownership check, error handling).

    Uses a callable for channel list so the broadcaster always reads the
    current state (channels may be swapped at runtime or in tests).
    """

    def __init__(
        self,
        channels: Callable[[], list[Channel]] | list[Channel],
        workspaces: Callable[[], dict[str, WorkspaceProfile]]
        | dict[str, WorkspaceProfile]
        | None = None,
    ) -> None:
        # Accept either a list or a callable returning a list.
        # Callable form ensures the broadcaster always reads the current channels
        # (important when the channel list may be swapped, e.g. in tests).
        self._get_channels: Callable[[], list[Channel]] = (
            channels if callable(channels) else lambda: channels
        )
        self._get_workspaces: Callable[[], dict[str, WorkspaceProfile]] = (
            workspaces if callable(workspaces) else lambda: workspaces or {}
        )

    # -- BusDeps protocol implementation --

    @property
    def channels(self) -> list[Channel]:
        """Return current channel list (satisfies BusDeps protocol)."""
        return self._get_channels()

    @property
    def workspaces(self) -> dict[str, WorkspaceProfile]:
        """Return current workspaces dict (satisfies BusDeps protocol)."""
        return self._get_workspaces()

    # -- Broadcast methods --

    async def broadcast_to_channels(
        self, jid: str, event: OutboundEvent, *, suppress_errors: bool = True
    ) -> None:
        """Send event to all connected channels.

        Delegates to ``sender.broadcast()`` — the single code path for channel
        iteration, JID resolution, ownership checks, and error handling.
        """
        await broadcast(self, jid, event, suppress_errors=suppress_errors)


class HostMessageBroadcaster:
    """Broadcasts host operational messages and stores them in message history.

    Uses separate store functions for host messages vs system notices so they
    get different message_type values in the DB. Host messages are invisible
    to the LLM; system notices are visible as pseudo-system user messages.
    """

    def __init__(
        self,
        broadcaster: MessageBroadcaster,
        store_host_fn: StoreMessageFn,
        store_notice_fn: StoreMessageFn,
        emit_event_fn: EmitEventFn,
    ) -> None:
        self.broadcaster = broadcaster
        self._store_host = store_host_fn
        self._store_notice = store_notice_fn
        self.emit_event = emit_event_fn

    async def _store_broadcast_and_emit(self, request: _StoreBroadcastAndEmitRequest) -> None:
        """Store a message, broadcast to channels, and emit an event.

        Shared implementation for broadcast_host_message and broadcast_system_notice.
        Each caller passes its own store_fn to control the message_type in the DB.
        """
        ts = datetime.now(UTC).isoformat()
        await request.store_fn(
            message_id=generate_message_id(request.id_prefix),
            chat_jid=request.chat_jid,
            sender=request.sender,
            sender_name=request.sender_name,
            content=request.text,
            timestamp=ts,
            is_from_me=True,
        )
        event = OutboundEvent(type=request.event_type, content=request.text)
        await self.broadcaster.broadcast_to_channels(request.chat_jid, event)
        self.emit_event(
            MessageEvent(
                chat_jid=request.chat_jid,
                sender_name=request.sender_name,
                content=request.text,
                timestamp=ts,
                is_bot=True,
            )
        )

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None:
        """Send operational notification from host/platform to user.

        Host messages are purely operational notifications (errors, status updates,
        confirmations) that are OUTSIDE the LLM's conversation. They are:
        - Sent to the user via channels
        - Stored in message history for user reference
        - NOT sent to the LLM as system messages or user messages
        - NOT part of the SDK conversation flow
        """
        await self._store_broadcast_and_emit(
            _StoreBroadcastAndEmitRequest(
                chat_jid=chat_jid,
                text=text,
                id_prefix="host",
                sender="host",
                sender_name="host",
                event_type=OutboundEventType.HOST,
                store_fn=self._store_host,
            )
        )

    async def broadcast_system_notice(self, chat_jid: str, text: str) -> None:
        """Store a system notice for delivery to the LLM.

        System notices are announcements from the host that the LLM needs to
        see (e.g. worktree updates, config changes). They are:
        - Stored in the DB as user messages so the polling loop delivers them
        - Included in conversation context for future container launches
        - Broadcast to channels with 📢 prefix for human visibility
        - Prefixed with [System Notice] so the LLM can distinguish from humans

        IMPORTANT: Only use for workspaces with an ongoing conversation (i.e.
        has_active_session is True). These messages persist in conversation
        history, so sending them to workspaces with no conversation (cleared
        or never started) creates stale spam that pollutes the next session.
        For those, use broadcast_host_message instead (human-visible only).
        See host_notify_worktree_updates() for the canonical routing pattern.
        """
        await self._store_broadcast_and_emit(
            _StoreBroadcastAndEmitRequest(
                chat_jid=chat_jid,
                text=f"[System Notice] {text}",
                id_prefix="sys-notice",
                sender="system_notice",
                sender_name="System",
                event_type=OutboundEventType.SYSTEM,
                store_fn=self._store_notice,
            )
        )


@dataclass(frozen=True)
class _StoreBroadcastAndEmitRequest:
    chat_jid: str
    text: str
    id_prefix: str
    sender: str
    sender_name: str
    event_type: OutboundEventType
    store_fn: StoreMessageFn


def find_admin_jid(groups: dict[str, WorkspaceProfile]) -> str:
    """Find the JID of the admin group from a groups dict.

    Returns the first admin group's JID, or empty string if none found.
    This is the single code path for all admin-group lookups — used by
    dep_factory, startup, shutdown, and IPC deploy handlers.
    """
    for jid, group in groups.items():
        if group.is_admin:
            return jid
    return ""


class SessionManager:
    """Manages agent session state."""

    def __init__(
        self,
        sessions_dict: dict[str, str],
        session_cleared_set: set[str],
    ) -> None:
        self._sessions = sessions_dict
        self._session_cleared = session_cleared_set

    def get_active_sessions(self, groups: dict[str, WorkspaceProfile]) -> dict[str, str]:
        """Build a {chat_jid: session_id} map from sessions and registered groups.

        ``self._sessions`` is keyed by group folder. This helper joins with the
        group registry (keyed by JID) to produce a JID-keyed mapping suitable
        for the deploy continuation file.

        Sessions that have been cleared (context reset) are excluded so deploy
        continuations don't inject resume messages for wiped sessions.
        """
        folder_to_jid: dict[str, str] = {g.folder: jid for jid, g in groups.items()}
        result: dict[str, str] = {}
        for folder, session_id in self._sessions.items():
            if folder in self._session_cleared:
                continue
            jid = folder_to_jid.get(folder, "")
            if jid and session_id:
                result[jid] = session_id
        return result

    def has_active_session(self, group_folder: str) -> bool:
        """Check if a group has an active (non-cleared) session."""
        return group_folder in self._sessions and group_folder not in self._session_cleared

    async def clear_session(self, group_folder: str) -> None:
        """Clear session state for a group."""
        self._sessions.pop(group_folder, None)
        self._session_cleared.add(group_folder)
        await clear_session(GroupFolder(group_folder))


class EventBusAdapter:
    """Provides event subscription with callback conversion."""

    def __init__(self, event_bus: EventBus) -> None:
        self.event_bus = event_bus

    def subscribe_events(
        self, callback: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> Callable[[], None]:
        """Subscribe to all event types and convert to callback format.

        Args:
            callback: async function that receives event dict

        Returns:
            unsubscribe function to cancel all subscriptions
        """
        unsubs = []

        async def on_msg(event: MessageEvent) -> None:
            await callback(
                {
                    "type": "message",
                    "chat_jid": event.chat_jid,
                    "sender_name": event.sender_name,
                    "content": event.content,
                    "timestamp": event.timestamp,
                    "is_bot": event.is_bot,
                }
            )

        async def on_activity(event: AgentActivityEvent) -> None:
            await callback(
                {
                    "type": "agent_activity",
                    "chat_jid": event.chat_jid,
                    "active": event.active,
                }
            )

        async def on_trace(event: AgentTraceEvent) -> None:
            await callback(
                {
                    "type": "agent_trace",
                    "chat_jid": event.chat_jid,
                    "trace_type": event.trace_type,
                    **event.data,
                }
            )

        async def on_clear(event: ChatClearedEvent) -> None:
            await callback({"type": "chat_cleared", "chat_jid": event.chat_jid})

        unsubs.append(self.event_bus.subscribe(MessageEvent, on_msg))
        unsubs.append(self.event_bus.subscribe(AgentActivityEvent, on_activity))
        unsubs.append(self.event_bus.subscribe(AgentTraceEvent, on_trace))
        unsubs.append(self.event_bus.subscribe(ChatClearedEvent, on_clear))

        def unsubscribe() -> None:
            for unsub in unsubs:
                unsub()

        return unsubscribe


class GroupMetadataManager:
    """Manages group chat metadata operations."""

    def __init__(
        self,
        groups_dict: dict[str, WorkspaceProfile],
        channels: list[Channel],
        get_available_groups_fn: Callable[[], Awaitable[list[dict[str, Any]]]],
    ) -> None:
        self._groups = groups_dict
        self._channels = channels
        self._get_available_groups = get_available_groups_fn

    def get_groups(self) -> list[dict[str, str]]:
        """Return list of registered groups for API."""
        return [{"jid": jid, "name": g.name, "folder": g.folder} for jid, g in self._groups.items()]

    async def get_available_groups(self) -> list[dict[str, Any]]:
        """Get list of all available groups."""
        return await self._get_available_groups()

    async def sync_group_metadata(self, *, force: bool) -> None:
        """Sync group metadata from channels."""
        for channel in self._channels:
            if hasattr(channel, "sync_group_metadata"):
                await channel.sync_group_metadata(force=force)

    def channels(self) -> list[Channel]:
        """Return all channels."""
        return self._channels

    def channels_connected(self) -> bool:
        """Check if any channel is connected."""
        return any(c.is_connected() for c in self._channels)


class PeriodicAgentManager:
    """Manages periodic agent configuration queries."""

    def __init__(self, groups_dict: dict[str, WorkspaceProfile]) -> None:
        self._groups = groups_dict

    async def get_periodic_agents(self) -> list[dict[str, Any]]:
        """Get status of scheduled agent tasks."""
        groups_by_folder = {group.folder: group for group in self._groups.values()}
        results = []
        for task in await get_all_tasks():
            if task.schedule_type != "cron":
                continue
            group = groups_by_folder.get(task.group_folder)
            if group is None:
                continue
            results.append(
                {
                    "name": group.name,
                    "folder": group.folder,
                    "schedule": task.schedule_value,
                    "context_mode": task.context_mode,
                    "last_run": task.last_run,
                    "next_run": task.next_run,
                    "status": task.status,
                }
            )
        return results


class UserMessageHandler:
    """Handles user message ingestion for TUI."""

    def __init__(
        self,
        ingest_message_fn: Callable[..., Awaitable[None]],
        enqueue_check_fn: Callable[[str], None],
    ) -> None:
        self._ingest_message = ingest_message_fn
        self._enqueue_check = enqueue_check_fn

    async def send_user_message(self, jid: str, content: str) -> None:
        """Send a user message from the TUI."""
        msg = NewMessage(
            id=generate_message_id("tui"),
            chat_jid=jid,
            sender="tui-user",
            sender_name="You",
            content=content,
            timestamp=datetime.now(UTC).isoformat(),
            is_from_me=False,
        )
        # Use unified ingestion to store, emit, AND broadcast to all channels
        await self._ingest_message(msg, source_channel="tui")
        self._enqueue_check(jid)

    async def get_messages(self, jid: str, limit: int) -> list[NewMessage]:
        """Get chat history for a group."""
        return await get_chat_history(jid, limit)


class GroupRegistrationManager:
    """Manages group registration operations."""

    def __init__(
        self,
        groups_dict: dict[str, WorkspaceProfile],
        register_workspace_fn: Callable[[WorkspaceProfile], Coroutine[Any, Any, None]],
        send_clear_confirmation_fn: Callable[[str], Awaitable[None]],
    ) -> None:
        self._groups = groups_dict
        self._register_workspace = register_workspace_fn
        self._send_clear_confirmation = send_clear_confirmation_fn

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        """Return all registered groups."""
        return self._groups

    def register_workspace(self, profile: WorkspaceProfile) -> None:
        """Register a workspace (async operation scheduled)."""
        create_background_task(
            self._register_workspace(profile),
            name=f"register-workspace-{profile.folder}",
        )

    async def clear_chat_history(self, chat_jid: str) -> None:
        """Clear chat history and send confirmation."""
        await self._send_clear_confirmation(chat_jid)
