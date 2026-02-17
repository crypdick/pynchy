"""Session and context lifecycle — reset, end, clear, redeploy, message ingestion.

Extracted from app.py to keep the orchestrator focused on wiring.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from pynchy.db import clear_session, set_chat_cleared_at, store_message
from pynchy.event_bus import ChatClearedEvent, MessageEvent
from pynchy.logger import logger
from pynchy.utils import create_background_task

if TYPE_CHECKING:
    from pynchy.group_queue import GroupQueue
    from pynchy.types import Channel, NewMessage, RegisteredGroup


class SessionDeps(Protocol):
    """Dependencies for session lifecycle operations."""

    @property
    def sessions(self) -> dict[str, str]: ...

    @property
    def _session_cleared(self) -> set[str]: ...

    @property
    def last_agent_timestamp(self) -> dict[str, str]: ...

    @property
    def queue(self) -> GroupQueue: ...

    @property
    def channels(self) -> list[Channel]: ...

    @property
    def registered_groups(self) -> dict[str, RegisteredGroup]: ...

    async def save_state(self) -> None: ...

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...

    def resolve_canonical_jid(self, jid: str) -> str: ...

    def get_channel_jid(self, canonical_jid: str, channel_name: str) -> str | None: ...

    def emit(self, event: Any) -> None: ...


async def handle_context_reset(
    deps: SessionDeps, chat_jid: str, group: RegisteredGroup, timestamp: str
) -> None:
    """Clear session state, merge worktree, and confirm context reset."""
    from pynchy.git_ops.worktree import merge_and_push_worktree
    from pynchy.workspace_config import has_project_access

    # Merge worktree commits before clearing session so work isn't stranded
    if has_project_access(group):
        create_background_task(
            asyncio.to_thread(merge_and_push_worktree, group.folder),
            name=f"worktree-merge-{group.folder}",
        )

    deps.sessions.pop(group.folder, None)
    deps._session_cleared.add(group.folder)
    await clear_session(group.folder)
    deps.queue.clear_pending_tasks(chat_jid)
    create_background_task(
        deps.queue.stop_active_process(chat_jid),
        name=f"stop-container-{chat_jid[:20]}",
    )
    deps.last_agent_timestamp[chat_jid] = timestamp
    await deps.save_state()
    await send_clear_confirmation(deps, chat_jid)


async def handle_end_session(
    deps: SessionDeps, chat_jid: str, group: RegisteredGroup, timestamp: str
) -> None:
    """Sync worktree and spin down the container without clearing context.

    Unlike context reset, this preserves conversation history. The next
    message will start a fresh container that picks up where it left off.
    """
    from pynchy.git_ops.worktree import merge_and_push_worktree
    from pynchy.workspace_config import has_project_access

    # Merge worktree commits before stopping so work isn't stranded
    if has_project_access(group):
        create_background_task(
            asyncio.to_thread(merge_and_push_worktree, group.folder),
            name=f"worktree-merge-{group.folder}",
        )

    # Stop the container but keep session state intact
    deps.queue.clear_pending_tasks(chat_jid)
    create_background_task(
        deps.queue.stop_active_process(chat_jid),
        name=f"stop-container-{chat_jid[:20]}",
    )
    deps.last_agent_timestamp[chat_jid] = timestamp
    await deps.save_state()
    await deps.broadcast_host_message(chat_jid, "👋")


async def send_clear_confirmation(deps: SessionDeps, chat_jid: str) -> None:
    """Set cleared_at, store and broadcast a system confirmation."""
    # Mark clear boundary — messages before this are hidden
    cleared_ts = datetime.now(UTC).isoformat()
    await set_chat_cleared_at(chat_jid, cleared_ts)
    deps.emit(ChatClearedEvent(chat_jid=chat_jid))

    await deps.broadcast_host_message(chat_jid, "🗑️")


async def trigger_manual_redeploy(deps: SessionDeps, chat_jid: str) -> None:
    """Handle a manual redeploy command — restart the service in-place."""
    from pynchy.adapters import SessionManager
    from pynchy.deploy import finalize_deploy
    from pynchy.git_ops.utils import get_head_sha

    sha = get_head_sha()
    logger.info("Manual redeploy triggered via magic word", chat_jid=chat_jid)

    # Build active_sessions so all groups resume after restart
    sm = SessionManager(deps.sessions, deps._session_cleared)
    active_sessions = sm.get_active_sessions(deps.registered_groups)

    await finalize_deploy(
        broadcast_host_message=deps.broadcast_host_message,
        chat_jid=chat_jid,
        commit_sha=sha,
        previous_sha=sha,
        active_sessions=active_sessions,
    )


async def ingest_user_message(
    deps: SessionDeps, msg: NewMessage, *, source_channel: str | None = None
) -> None:
    """Unified user message ingestion — stores, emits, and broadcasts to all channels.

    This is the common code path for ALL user inputs from ANY UI:
    - Channel messages
    - TUI messages
    - Any future channels

    Args:
        deps: Session dependencies
        msg: The user message to ingest
        source_channel: Optional name of the originating channel (e.g., "tui").
                       If provided, we skip broadcasting back to that channel.
    """
    # 1. Store in database
    await store_message(msg)

    # 2. Emit to event bus (for TUI/SSE, logging, etc.)
    deps.emit(
        MessageEvent(
            chat_jid=msg.chat_jid,
            sender_name=msg.sender_name,
            content=msg.content,
            timestamp=msg.timestamp,
            is_bot=False,
        )
    )

    # 3. Broadcast to all connected channels (except source)
    # This ensures messages from one UI appear in all other UIs
    for ch in deps.channels:
        if ch.is_connected():
            # Skip broadcasting back to the source channel
            if source_channel and ch.name == source_channel:
                continue

            # Use channel-specific alias JID if one exists
            target_jid = deps.get_channel_jid(msg.chat_jid, ch.name) or msg.chat_jid

            # Format the message with sender name
            formatted = f"{msg.sender_name}: {msg.content}"
            try:
                await ch.send_message(target_jid, formatted)
            except (OSError, TimeoutError, ConnectionError) as exc:
                logger.warning("Cross-channel broadcast failed", channel=ch.name, err=str(exc))


async def on_inbound(deps: SessionDeps, _jid: str, msg: NewMessage) -> None:
    """Handle inbound message from any channel — delegates to unified ingestion."""
    # Find which channel this came from
    source_channel = None
    for ch in deps.channels:
        if ch.owns_jid(msg.chat_jid):
            source_channel = ch.name
            break

    # Resolve alias JID to canonical so the message is stored under the
    # workspace's primary JID (the one in registered_groups).
    canonical = deps.resolve_canonical_jid(msg.chat_jid)
    if canonical != msg.chat_jid:
        logger.debug(
            "Resolved alias JID to canonical",
            alias=msg.chat_jid,
            canonical=canonical,
        )
        msg = replace(msg, chat_jid=canonical)

    await ingest_user_message(deps, msg, source_channel=source_channel)
