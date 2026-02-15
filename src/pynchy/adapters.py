"""Dependency adapters for subsystem integration.

Provides concrete implementations of Protocol interfaces used by task scheduler,
HTTP server, and IPC watcher. Reduces boilerplate delegation code in PynchyApp.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pynchy.config import GOD_GROUP_FOLDER
from pynchy.db import clear_session, get_active_task_for_group, get_chat_history
from pynchy.router import format_outbound

if TYPE_CHECKING:
    from pynchy.event_bus import EventBus
    from pynchy.group_queue import GroupQueue
    from pynchy.types import Channel, NewMessage, RegisteredGroup


class MessageBroadcaster:
    """Broadcasts messages to all connected channels.

    The public API is on HostMessageBroadcaster — typed methods with correct
    emoji prefixes and DB persistence. Raw channel sends are private.
    """

    def __init__(self, channels: list[Channel]) -> None:
        self.channels = channels

    async def _broadcast_to_channels(self, jid: str, text: str) -> None:
        """Send message to all connected channels (internal use)."""
        for ch in self.channels:
            if ch.is_connected():
                with contextlib.suppress(Exception):
                    await ch.send_message(jid, text)

    async def _broadcast_formatted(self, jid: str, raw_text: str) -> None:
        """Send message with per-channel formatting (internal use)."""
        for ch in self.channels:
            if ch.is_connected():
                text = format_outbound(ch, raw_text)
                if text:
                    with contextlib.suppress(Exception):
                        await ch.send_message(jid, text)


class HostMessageBroadcaster:
    """Broadcasts host operational messages and stores them in message history."""

    def __init__(
        self,
        broadcaster: MessageBroadcaster,
        store_message_fn: Any,  # async (id, jid, sender, content, timestamp) -> None
        emit_event_fn: Any,  # (MessageEvent) -> None
    ) -> None:
        self.broadcaster = broadcaster
        self.store_message = store_message_fn
        self.emit_event = emit_event_fn

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None:
        """Send operational notification from host/platform to user.

        Host messages are purely operational notifications (errors, status updates,
        confirmations) that are OUTSIDE the LLM's conversation. They are:
        - Sent to the user via channels (WhatsApp, etc.)
        - Stored in message history for user reference
        - NOT sent to the LLM as system messages or user messages
        - NOT part of the SDK conversation flow
        """
        from pynchy.event_bus import MessageEvent

        ts = datetime.now(UTC).isoformat()
        await self.store_message(
            id=f"host-{int(datetime.now(UTC).timestamp() * 1000)}",
            chat_jid=chat_jid,
            sender="host",
            sender_name="host",
            content=text,
            timestamp=ts,
            is_from_me=True,
        )
        channel_text = f"\U0001f3e0 {text}"
        await self.broadcaster._broadcast_to_channels(chat_jid, channel_text)
        self.emit_event(
            MessageEvent(
                chat_jid=chat_jid,
                sender_name="host",
                content=text,
                timestamp=ts,
                is_bot=True,
            )
        )

    async def broadcast_system_notice(self, chat_jid: str, text: str) -> None:
        """Store a system notice for delivery to the LLM.

        System notices are announcements from the host that the LLM needs to
        see (e.g. worktree updates, config changes). They are:
        - Stored in the DB so the polling loop delivers them to running agents
        - Included in conversation context for future container launches
        - Broadcast to channels with 📢 prefix for human visibility
        """
        from pynchy.event_bus import MessageEvent

        ts = datetime.now(UTC).isoformat()
        await self.store_message(
            id=f"sys-notice-{int(datetime.now(UTC).timestamp() * 1000)}",
            chat_jid=chat_jid,
            sender="system_notice",
            sender_name="system_notice",
            content=text,
            timestamp=ts,
            is_from_me=True,
        )
        channel_text = f"\U0001f4e2 {text}"
        await self.broadcaster._broadcast_to_channels(chat_jid, channel_text)
        self.emit_event(
            MessageEvent(
                chat_jid=chat_jid,
                sender_name="system_notice",
                content=text,
                timestamp=ts,
                is_bot=True,
            )
        )


class GroupRegistry:
    """Manages registered group lookup and metadata."""

    def __init__(self, groups_dict: dict[str, RegisteredGroup]) -> None:
        self._groups = groups_dict

    def registered_groups(self) -> dict[str, RegisteredGroup]:
        """Return all registered groups."""
        return self._groups

    def god_chat_jid(self) -> str:
        """Find the JID of the god group."""
        for jid, group in self._groups.items():
            if group.folder == GOD_GROUP_FOLDER:
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

    def get_sessions(self) -> dict[str, str]:
        """Return all active sessions."""
        return self._sessions

    async def clear_session(self, group_folder: str) -> None:
        """Clear session state for a group."""
        self._sessions.pop(group_folder, None)
        self._session_cleared.add(group_folder)
        await clear_session(group_folder)


class QueueManager:
    """Manages message queue operations."""

    def __init__(self, queue: GroupQueue) -> None:
        self._queue = queue

    @property
    def queue(self) -> GroupQueue:
        """Return the group queue."""
        return self._queue

    def enqueue_message_check(self, group_jid: str) -> None:
        """Enqueue a message check for a group."""
        self._queue.enqueue_message_check(group_jid)

    def on_process(self, group_jid: str, proc: Any, container_name: str, group_folder: str) -> None:
        """Register a container process with the queue."""
        self._queue.register_process(group_jid, proc, container_name, group_folder)


class EventBusAdapter:
    """Provides event subscription with callback conversion."""

    def __init__(self, event_bus: EventBus) -> None:
        self.event_bus = event_bus

    def subscribe_events(self, callback: Any) -> Any:
        """Subscribe to all event types and convert to callback format.

        Args:
            callback: async function that receives event dict

        Returns:
            unsubscribe function to cancel all subscriptions
        """
        from pynchy.event_bus import (
            AgentActivityEvent,
            AgentTraceEvent,
            ChatClearedEvent,
            MessageEvent,
        )

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
        groups_dict: dict[str, RegisteredGroup],
        channels: list[Channel],
        get_available_groups_fn: Any,
    ) -> None:
        self._groups = groups_dict
        self._channels = channels
        self._get_available_groups = get_available_groups_fn

    def get_groups(self) -> list[dict[str, Any]]:
        """Return list of registered groups for API."""
        return [{"jid": jid, "name": g.name, "folder": g.folder} for jid, g in self._groups.items()]

    async def get_available_groups(self) -> list[Any]:
        """Get list of all available groups."""
        return await self._get_available_groups()

    async def sync_group_metadata(self, force: bool) -> None:
        """Sync group metadata from channels."""
        for channel in self._channels:
            if hasattr(channel, "sync_group_metadata"):
                await channel.sync_group_metadata(force)

    def channels(self) -> list[Channel]:
        """Return all channels."""
        return self._channels

    def channels_connected(self) -> bool:
        """Check if any channel is connected."""
        return any(c.is_connected() for c in self._channels)


class PeriodicAgentManager:
    """Manages periodic agent configuration queries."""

    def __init__(self, groups_dict: dict[str, RegisteredGroup]) -> None:
        self._groups = groups_dict

    async def get_periodic_agents(self) -> list[dict[str, Any]]:
        """Get status of all periodic agents."""
        from pynchy.periodic import load_periodic_config

        results = []
        for group in self._groups.values():
            config = load_periodic_config(group.folder)
            if config is None:
                continue
            task = await get_active_task_for_group(group.folder)
            results.append(
                {
                    "name": group.name,
                    "folder": group.folder,
                    "schedule": config.schedule,
                    "context_mode": config.context_mode,
                    "last_run": task.last_run if task else None,
                    "next_run": task.next_run if task else None,
                    "status": task.status if task else "no_task",
                }
            )
        return results


class UserMessageHandler:
    """Handles user message ingestion for TUI."""

    def __init__(
        self,
        ingest_message_fn: Any,  # async (msg, source_channel) -> None
        enqueue_check_fn: Any,  # (jid) -> None
    ) -> None:
        self._ingest_message = ingest_message_fn
        self._enqueue_check = enqueue_check_fn

    async def send_user_message(self, jid: str, content: str) -> None:
        """Send a user message from the TUI."""
        from pynchy.types import NewMessage

        msg = NewMessage(
            id=f"tui-{int(datetime.now(UTC).timestamp() * 1000)}",
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
        groups_dict: dict[str, RegisteredGroup],
        register_group_fn: Any,  # async (jid, group) -> None
        send_clear_confirmation_fn: Any,  # async (jid) -> None
    ) -> None:
        self._groups = groups_dict
        self._register_group = register_group_fn
        self._send_clear_confirmation = send_clear_confirmation_fn

    def registered_groups(self) -> dict[str, RegisteredGroup]:
        """Return all registered groups."""
        return self._groups

    def register_group(self, jid: str, group: RegisteredGroup) -> None:
        """Register a new group (async operation scheduled)."""
        asyncio.ensure_future(self._register_group(jid, group))

    async def clear_chat_history(self, chat_jid: str) -> None:
        """Clear chat history and send confirmation."""
        await self._send_clear_confirmation(chat_jid)
