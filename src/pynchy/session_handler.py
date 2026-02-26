"""Session and context lifecycle — reset, end, clear, redeploy, message ingestion.

Extracted from app.py to keep the orchestrator focused on wiring.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from pynchy.chat.bus import broadcast
from pynchy.container_runner._session import destroy_session
from pynchy.db import clear_session, set_chat_cleared_at, store_message
from pynchy.event_bus import ChatClearedEvent, MessageEvent
from pynchy.git_ops.worktree import background_merge_worktree
from pynchy.logger import logger
from pynchy.utils import create_background_task

if TYPE_CHECKING:
    from pynchy.group_queue import GroupQueue
    from pynchy.types import Channel, NewMessage, WorkspaceProfile


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
    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    async def save_state(self) -> None: ...

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...

    def resolve_canonical_jid(self, jid: str) -> str: ...

    def get_channel_jid(self, canonical_jid: str, channel_name: str) -> str | None: ...

    def emit(self, event: Any) -> None: ...


async def _teardown_group(
    deps: SessionDeps,
    group: WorkspaceProfile,
    chat_jid: str,
    timestamp: str,
    *,
    clear_context: bool = False,
) -> None:
    """Shared teardown for context reset and end session.

    Merges worktree, destroys the persistent session, stops containers,
    advances the cursor, and persists state.  When *clear_context* is True,
    also wipes the session from memory and DB (full context reset).
    """
    logger.info("teardown_trace", step="start", group=group.name, clear_context=clear_context)

    # Merge worktree commits before killing the container so work isn't stranded
    background_merge_worktree(group)

    # Destroy persistent session (kills container)
    create_background_task(
        destroy_session(group.folder),
        name=f"destroy-session-{group.folder}",
    )

    if clear_context:
        deps.sessions.pop(group.folder, None)
        deps._session_cleared.add(group.folder)
        logger.info("teardown_trace", step="clear_session_start", group=group.name)
        await clear_session(group.folder)
        logger.info("teardown_trace", step="clear_session_done", group=group.name)

    deps.queue.clear_pending_tasks(chat_jid)
    create_background_task(
        deps.queue.stop_active_process(chat_jid),
        name=f"stop-container-{chat_jid[:20]}",
    )
    deps.last_agent_timestamp[chat_jid] = timestamp
    logger.info("teardown_trace", step="save_state_start", group=group.name)
    await deps.save_state()
    logger.info("teardown_trace", step="done", group=group.name)


async def handle_context_reset(
    deps: SessionDeps, chat_jid: str, group: WorkspaceProfile, timestamp: str
) -> None:
    """Clear session state, merge worktree, destroy session, and confirm context reset."""
    await _teardown_group(deps, group, chat_jid, timestamp, clear_context=True)
    logger.info("teardown_trace", step="send_clear_confirmation_start", group=group.name)
    await send_clear_confirmation(deps, chat_jid)
    logger.info("teardown_trace", step="send_clear_confirmation_done", group=group.name)


async def handle_end_session(
    deps: SessionDeps, chat_jid: str, group: WorkspaceProfile, timestamp: str
) -> None:
    """Sync worktree and spin down the container without clearing context.

    Unlike context reset, this preserves conversation history. The next
    message will start a fresh container that picks up where it left off.
    """
    await _teardown_group(deps, group, chat_jid, timestamp)
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
    active_sessions = sm.get_active_sessions(deps.workspaces)

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
    # This ensures messages from one UI appear in all other UIs.
    # Include sender attribution so the message isn't mistaken for bot
    # output (e.g. Slack posts as the bot user).  The source channel is
    # skipped, so magic-word detection on the originating channel is
    # unaffected — and receiving channels won't re-ingest bot-posted
    # messages (Slack filters bot_id, WhatsApp filters IsFromMe echoes).
    channel_text = f"[{msg.sender_name}] {msg.content}"
    await broadcast(
        deps, msg.chat_jid, channel_text, skip_channel=source_channel, source="cross_post"
    )


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

    # Check channel access mode — skip inbound from write-only channels
    group = deps.workspaces.get(msg.chat_jid)
    if group and source_channel:
        from pynchy.config_access import resolve_channel_config, resolve_workspace_connection_name

        expected = resolve_workspace_connection_name(group.folder)
        if expected and expected != source_channel:
            logger.debug(
                "Ignoring inbound from non-owning channel",
                channel=source_channel,
                expected=expected,
                chat_jid=msg.chat_jid,
            )
            return

        resolved = resolve_channel_config(
            group.folder,
            channel_jid=msg.chat_jid,
            channel_plugin_name=source_channel,
        )
        if resolved.access == "write":
            logger.debug(
                "Ignoring inbound from write-only channel",
                channel=source_channel,
                chat_jid=msg.chat_jid,
            )
            return

        # Read receipt: react with 👀 so the sender knows pynchy received
        # the message.  Only on channels with write access (readwrite).
        if resolved.access == "readwrite":
            from pynchy.chat.channel_handler import send_reaction_to_channels

            create_background_task(
                send_reaction_to_channels(deps, msg.chat_jid, msg.id, msg.sender, "eyes"),
                name=f"read-receipt-{msg.id}",
            )

    await ingest_user_message(deps, msg, source_channel=source_channel)
