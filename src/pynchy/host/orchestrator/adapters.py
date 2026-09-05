"""Dependency adapters for subsystem integration.

Provides concrete implementations of Protocol interfaces used by task scheduler,
HTTP server, and IPC watcher. Reduces boilerplate delegation code in PynchyApp.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from pynchy.agent_protocol.api import CheckpointControlState
from pynchy.event_bus import MessageEvent
from pynchy.host.orchestrator.messaging.sender import broadcast
from pynchy.logger import logger
from pynchy.plugins.api import (
    Channel,
    OutboundEvent,
    OutboundEventType,
)
from pynchy.state.api import (
    get_in_flight_turns,
    is_chat_paused,
    store_message_direct,
)
from pynchy.workspace.api import (
    WorkspaceProfile,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)

# Type aliases for callback signatures used across adapters
StoreMessageFn = Callable[..., Awaitable[None]]
EmitEventFn = Callable[..., None]
PauseStateFn = Callable[[str], Awaitable[bool]]


def _generate_message_id(prefix: str) -> str:
    """Generate the timestamp-shaped identifiers used for host-authored messages."""
    return f"{prefix}-{int(datetime.now(UTC).timestamp() * 1000)}"


class MessageBroadcaster:
    """Broadcasts messages to all connected channels.

    Satisfies the ``BusDeps`` protocol from ``messaging.sender`` so that
    ``broadcast_to_channels`` delegates to the single ``sender.broadcast()``
    code path (JID resolution, ownership check, error handling).

    Uses a callable for channel list so the broadcaster always reads the
    current state (channels may be swapped at runtime or in tests).
    """

    def __init__(
        self,
        channels: Callable[[], list[Channel]] | list[Channel],
    ) -> None:
        # Accept either a list or a callable returning a list.
        # Callable form ensures the broadcaster always reads the current channels
        # (important when the channel list may be swapped, e.g. in tests).
        self._get_channels: Callable[[], list[Channel]] = (
            channels if callable(channels) else lambda: channels
        )

    # -- BusDeps protocol implementation --

    @property
    def channels(self) -> list[Channel]:
        """Return current channel list (satisfies BusDeps protocol)."""
        return self._get_channels()

    # -- Broadcast methods --

    async def broadcast_to_channels(
        self, jid: str, event: OutboundEvent, *, suppress_errors: bool = True
    ) -> None:
        """Send event to all connected channels.

        Delegates to ``sender.broadcast()`` — the single code path for channel
        iteration, JID resolution, ownership checks, and error handling.
        """
        await broadcast(self, jid, event, suppress_errors=suppress_errors)

    async def broadcast_synthetic_user_input(self, jid: str, content: str) -> None:
        """Send one canary through the ordinary channel delivery path."""
        await self.broadcast_to_channels(
            jid,
            OutboundEvent(
                type=OutboundEventType.TEXT,
                content=content,
                metadata={"synthetic_user_input": True},
            ),
        )


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
        is_chat_paused: PauseStateFn,
    ) -> None:
        self.broadcaster = broadcaster
        self._store_host = store_host_fn
        self._store_notice = store_notice_fn
        self.emit_event = emit_event_fn
        self._is_chat_paused = is_chat_paused

    async def _store_broadcast_and_emit(self, request: _StoreBroadcastAndEmitRequest) -> None:
        """Store a message, broadcast to channels, and emit an event.

        Shared implementation for broadcast_host_message and broadcast_system_notice.
        Each caller passes its own store_fn to control the message_type in the DB.
        """
        ts = datetime.now(UTC).isoformat()
        await request.store_fn(
            message_id=_generate_message_id(request.id_prefix),
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
        if await self._is_chat_paused(chat_jid):
            logger.info("Suppressed system notice for paused chat", chat_jid=chat_jid)
            return
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


def make_host_message_broadcaster(
    broadcaster: MessageBroadcaster,
    emit_event: EmitEventFn,
) -> HostMessageBroadcaster:
    """Wire host broadcasting to its two message-history representations."""

    async def store_host_message(**kwargs: object) -> None:
        await _store_broadcast_message(kwargs, message_type="host")

    async def store_system_notice(**kwargs: object) -> None:
        await _store_broadcast_message(kwargs, message_type="user")

    return HostMessageBroadcaster(
        broadcaster,
        store_host_message,
        store_system_notice,
        emit_event,
        _is_chat_paused,
    )


async def _is_chat_paused(chat_jid: str) -> bool:
    if await is_chat_paused(chat_jid):
        return True
    paused_states = {
        CheckpointControlState.PAUSE_REQUESTED,
        CheckpointControlState.PAUSED,
    }
    return any(
        turn.chat_jid == chat_jid and turn.control_state in paused_states
        for turn in await get_in_flight_turns()
    )


async def _store_broadcast_message(
    kwargs: dict[str, object],
    *,
    message_type: str,
) -> None:
    await store_message_direct(
        message_id=str(kwargs["message_id"]),
        chat_jid=str(kwargs["chat_jid"]),
        sender=str(kwargs["sender"]),
        sender_name=str(kwargs["sender_name"]) if kwargs.get("sender_name") else "",
        content=str(kwargs["content"]),
        timestamp=str(kwargs["timestamp"]),
        is_from_me=bool(kwargs.get("is_from_me", True)),
        message_type=message_type,
        metadata={"source": "host_broadcaster"},
    )


def resolve_admin_notification_jid(
    groups: dict[str, WorkspaceProfile], admin_workspace: str | None
) -> str:
    """Resolve the configured admin workspace for host lifecycle messages.

    The target must resolve to a registered admin workspace. Invalid or absent
    configuration suppresses the notification rather than sending it elsewhere.
    """
    if admin_workspace is None:
        logger.error("Admin notification workspace is not configured")
        return ""

    workspace = next(
        (profile for profile in groups.values() if profile.folder == admin_workspace), None
    )
    if workspace is None:
        logger.error(
            "Configured admin notification workspace is not registered",
            folder=admin_workspace,
        )
        return ""
    if not workspace.is_admin:
        logger.error(
            "Configured admin notification workspace is not an admin",
            folder=admin_workspace,
        )
        return ""
    return workspace.jid


class SessionManager:
    """Projects active sessions; context resets belong to session_handler."""

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
