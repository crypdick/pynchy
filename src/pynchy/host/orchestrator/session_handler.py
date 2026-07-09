"""Session and context lifecycle — reset, end, clear, redeploy, message ingestion.

Extracted from app.py to keep the orchestrator focused on wiring.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pynchy.event_bus import ChatClearedEvent, Event, MessageEvent
from pynchy.host.container_manager.session import destroy_session
from pynchy.host.git_ops._worktree_merge import background_merge_worktree
from pynchy.host.orchestrator.messaging.cursor import advance_cursor
from pynchy.host.orchestrator.messaging.sender import broadcast
from pynchy.logger import logger
from pynchy.state import clear_session, set_chat_cleared_at, store_message
from pynchy.types import Channel, GroupFolder, NewMessage, WorkspaceProfile
from pynchy.utils import create_background_task

if TYPE_CHECKING:
    from pynchy.host.orchestrator.concurrency import GroupQueue


@runtime_checkable
class SessionDeps(Protocol):
    """Dependencies for session lifecycle operations."""

    @property
    def sessions(self) -> dict[str, str]: ...

    @property
    def session_cleared(self) -> set[str]: ...

    @property
    def last_agent_timestamp(self) -> dict[str, str]: ...

    @property
    def queue(self) -> GroupQueue: ...

    @property
    def channels(self) -> list[Channel]: ...

    @property
    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    async def register_workspace(self, profile: WorkspaceProfile) -> None: ...

    async def save_state(self) -> None: ...

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...

    def emit(self, event: Event) -> None: ...


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
        deps.session_cleared.add(group.folder)
        logger.info("teardown_trace", step="clear_session_start", group=group.name)
        await clear_session(GroupFolder(group.folder))
        logger.info("teardown_trace", step="clear_session_done", group=group.name)

    deps.queue.clear_pending_tasks(chat_jid)
    create_background_task(
        deps.queue.stop_active_process(chat_jid),
        name=f"stop-container-{chat_jid[:20]}",
    )
    logger.info("teardown_trace", step="save_state_start", group=group.name)
    await advance_cursor(deps, chat_jid, timestamp)
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
    """Handle a manual redeploy command through Temporal."""
    from pynchy.host.git_ops.utils import get_head_sha
    from pynchy.host.orchestrator.adapters import SessionManager
    from pynchy.host.orchestrator.temporal.deploy import DeployRequest
    from pynchy.host.orchestrator.temporal.scheduler import start_deploy_workflow

    sha = get_head_sha()
    logger.info("Manual redeploy triggered via magic word", chat_jid=chat_jid)

    # Build active_sessions so all groups resume after restart
    sm = SessionManager(deps.sessions, deps.session_cleared)
    active_sessions = sm.get_active_sessions(deps.workspaces)

    await start_deploy_workflow(
        DeployRequest(
            chat_jid=chat_jid,
            commit_sha=sha,
            previous_sha=sha,
            active_sessions=active_sessions,
            rebuild=False,
            reason="manual_redeploy",
        )
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
    from pynchy.types import OutboundEvent, OutboundEventType

    channel_text = f"[{msg.sender_name}] {msg.content}"
    await broadcast(
        deps,
        msg.chat_jid,
        OutboundEvent(type=OutboundEventType.TEXT, content=channel_text),
        skip_channel=source_channel,
        source="cross_post",
    )


async def on_inbound(deps: SessionDeps, _jid: str, msg: NewMessage) -> None:
    """Handle inbound message from any channel — delegates to unified ingestion."""
    # Find which channel this came from
    source_channel = None
    for ch in deps.channels:
        if ch.owns_jid(msg.chat_jid):
            source_channel = ch.name
            break

    await _ensure_dynamic_thread_workspace(deps, msg)

    # Check channel ownership. Inbound handling is permissive once the channel
    # owns this workspace.
    group = deps.workspaces.get(msg.chat_jid)
    if group and source_channel:
        from pynchy.config.access import resolve_workspace_connection_name

        expected = resolve_workspace_connection_name(group.folder)
        if expected and expected != source_channel:
            logger.debug(
                "Ignoring inbound from non-owning channel",
                channel=source_channel,
                expected=expected,
                chat_jid=msg.chat_jid,
            )
            return

        from pynchy.host.orchestrator.messaging.channel_handler import send_reaction_to_channels

        create_background_task(
            send_reaction_to_channels(deps, msg.chat_jid, msg.id, msg.sender, "eyes"),
            name=f"read-receipt-{msg.id}",
        )

    await ingest_user_message(deps, msg, source_channel=source_channel)


async def _ensure_dynamic_thread_workspace(deps: SessionDeps, msg: NewMessage) -> None:
    """Register a Discord thread workspace that inherits its parent profile."""
    if msg.chat_jid in deps.workspaces:
        return
    metadata = msg.metadata or {}
    parent_jid = metadata.get("discord_parent_chat_jid")
    if not isinstance(parent_jid, str):
        return
    parent = deps.workspaces.get(parent_jid)
    if parent is None:
        return

    from pynchy.host.orchestrator.workspace_config import dynamic_thread_folder

    thread_name = metadata.get("discord_channel_name")
    if not isinstance(thread_name, str) or not thread_name.strip():
        thread_name = msg.chat_jid
    profile = WorkspaceProfile(
        jid=msg.chat_jid,
        name=f"{parent.name}/{thread_name}",
        folder=dynamic_thread_folder(parent.folder, msg.chat_jid),
        trigger=parent.trigger,
        container_config=parent.container_config,
        security=parent.security,
        is_admin=parent.is_admin,
        added_at=datetime.now(UTC).isoformat(),
    )
    await deps.register_workspace(profile)
    logger.info(
        "Registered dynamic thread workspace",
        jid=msg.chat_jid,
        parent_jid=parent_jid,
        folder=profile.folder,
    )
