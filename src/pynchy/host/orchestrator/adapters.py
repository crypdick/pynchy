"""Dependency adapters for subsystem integration.

Provides concrete implementations of Protocol interfaces used by task scheduler,
HTTP server, and IPC watcher. Reduces boilerplate delegation code in PynchyApp.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable
from uuid import uuid4

from pynchy.agent_protocol.api import CheckpointControlState
from pynchy.event_bus import MessageEvent
from pynchy.host.orchestrator.messaging.sender import BusDeps, broadcast
from pynchy.logger import logger
from pynchy.plugins.api import (
    OutboundEvent,
    OutboundEventType,
)
from pynchy.state.api import (
    get_in_flight_turns,
    is_chat_paused,
    store_message_direct,
)
from pynchy.workspace.api import (
    WorkspaceProfile,
)


@runtime_checkable
class HostMessageDeps(BusDeps, Protocol):
    """Channels and event publication used by host-authored notifications."""

    def emit(self, event: MessageEvent) -> None: ...


async def broadcast_host_message(deps: HostMessageDeps, chat_jid: str, text: str) -> None:
    """Store and send an operational message outside the agent conversation."""
    await _store_broadcast_and_emit(
        deps, chat_jid, OutboundEvent(type=OutboundEventType.HOST, content=text)
    )


async def broadcast_system_notice(deps: HostMessageDeps, chat_jid: str, text: str) -> None:
    """Store an agent-visible notice and send it to the channel unless paused.

    Call only for an active conversation: notices enter the next agent input.
    Use a host message for workspaces with no conversation, to avoid stale input.
    """
    if await _is_chat_paused(chat_jid):
        logger.info("Suppressed system notice for paused chat", chat_jid=chat_jid)
        return
    await _store_broadcast_and_emit(
        deps,
        chat_jid,
        OutboundEvent(type=OutboundEventType.SYSTEM, content=f"[System Notice] {text}"),
    )


async def _store_broadcast_and_emit(
    deps: HostMessageDeps, chat_jid: str, event: OutboundEvent
) -> None:
    notice = event.type is OutboundEventType.SYSTEM
    sender_name = "System" if notice else "host"
    id_prefix = "sys-notice" if notice else "host"
    timestamp = datetime.now(UTC).isoformat()
    # Distinct notifications can share a timestamp; their IDs must not collide.
    await store_message_direct(
        message_id=f"{id_prefix}-{uuid4().hex}",
        chat_jid=chat_jid,
        sender="system_notice" if notice else "host",
        sender_name=sender_name,
        content=event.content,
        timestamp=timestamp,
        is_from_me=True,
        message_type="user" if notice else "host",
        metadata={"source": "host_broadcaster"},
    )
    await broadcast(deps, chat_jid, event)
    deps.emit(
        MessageEvent(
            chat_jid=chat_jid,
            sender_name=sender_name,
            content=event.content,
            timestamp=timestamp,
            is_bot=True,
        )
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


def get_active_sessions(
    sessions: dict[str, str],
    session_cleared: set[str],
    groups: dict[str, WorkspaceProfile],
) -> dict[str, str]:
    """Join folder-keyed sessions to JIDs for deploy continuations.

    Exclude cleared sessions so deploys cannot resume wiped context.
    """
    folder_to_jid = {g.folder: jid for jid, g in groups.items()}
    return {
        jid: session_id
        for folder, session_id in sessions.items()
        if folder not in session_cleared and (jid := folder_to_jid.get(folder)) and session_id
    }


def has_active_session(
    sessions: dict[str, str], session_cleared: set[str], group_folder: str
) -> bool:
    """Check for a non-cleared session binding, including an empty session ID."""
    return group_folder in sessions and group_folder not in session_cleared
