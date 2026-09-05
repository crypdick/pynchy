"""Session and context lifecycle — reset, end, clear, redeploy, message ingestion.

Extracted from app.py to keep the orchestrator focused on wiring.
"""

from __future__ import annotations

from collections.abc import (  # noqa: TC003 - beartype resolves teardown strategy annotations at runtime.
    Awaitable,
    Callable,
)
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pynchy.async_tasks import create_background_task
from pynchy.conversation.api import dynamic_thread_folder, notify_conversation_delivery_completed
from pynchy.event_bus import ChatClearedEvent, Event, MessageEvent
from pynchy.host.orchestrator.messaging.channel_handler import send_reaction_to_channels
from pynchy.host.orchestrator.messaging.cursor import advance_cursor
from pynchy.host.orchestrator.messaging.sender import broadcast
from pynchy.host.orchestrator.temporal.api import DeployRequest, start_deploy_workflow
from pynchy.identifiers import (
    GroupFolder,
    RuntimeId,
)
from pynchy.logger import logger
from pynchy.plugins.api import (
    Channel,
    NewMessage,
    OutboundEvent,
    OutboundEventType,
)
from pynchy.state.api import clear_session, set_chat_cleared_at, store_message
from pynchy.workspace.api import WorkspaceProfile

if TYPE_CHECKING:
    from pynchy.host.orchestrator.concurrency import GroupQueue


@runtime_checkable
class ContextResetDeps(Protocol):
    """Minimal collaborators needed to discard durable agent context."""

    @property
    def sessions(self) -> dict[str, str]: ...

    @property
    def session_cleared(self) -> set[str]: ...

    async def prepare_context_reset(self, group: WorkspaceProfile) -> None: ...

    async def destroy_runtime_session(self, group_folder: str) -> None: ...


@runtime_checkable
class SessionDeps(Protocol):
    """Dependencies for the complete session lifecycle."""

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

    def current_deploy_revision(self) -> tuple[str, str]: ...


@runtime_checkable
class ResetSessionDeps(SessionDeps, ContextResetDeps, Protocol):
    """Session collaborators that also participate in destructive reset."""


async def clear_durable_context(
    deps: ContextResetDeps,
    group: WorkspaceProfile,
) -> None:
    """Clear every session-owned runtime and security reference."""
    await deps.prepare_context_reset(group)
    await deps.destroy_runtime_session(group.folder)
    deps.sessions.pop(group.folder, None)
    deps.session_cleared.add(group.folder)
    await clear_session(GroupFolder(group.folder))


async def _teardown_group(
    deps: SessionDeps,
    group: WorkspaceProfile,
    chat_jid: str,
    timestamp: str,
    *,
    reset_context: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Shared teardown for context reset and end session.

    Destroys the persistent session, stops containers, advances the cursor, and
    persists state. A reset action composes plugin settlement and durable
    cleanup into that shared teardown sequence.
    """
    logger.info(
        "teardown_trace",
        step="start",
        group=group.name,
        resets_context=reset_context is not None,
    )

    runtime_id = RuntimeId(group.folder)
    if reset_context is not None:
        await deps.queue.stop_active_process_for_control(RuntimeId(group.folder))
    else:
        create_background_task(
            deps.queue.destroy_runtime_session(runtime_id),
            name=f"destroy-session-{group.folder}",
        )
    if reset_context is not None:
        logger.info("teardown_trace", step="clear_session_start", group=group.name)
        await reset_context()
        logger.info("teardown_trace", step="clear_session_done", group=group.name)

    deps.queue.clear_pending_tasks(runtime_id)
    if reset_context is None:
        create_background_task(
            deps.queue.stop_active_process(runtime_id),
            name=f"stop-container-{chat_jid[:20]}",
        )
    logger.info("teardown_trace", step="save_state_start", group=group.name)
    await advance_cursor(deps, chat_jid, timestamp)
    logger.info("teardown_trace", step="done", group=group.name)


async def handle_context_reset(
    deps: ResetSessionDeps,
    chat_jid: str,
    group: WorkspaceProfile,
    timestamp: str,
    *,
    source_message: NewMessage | None = None,
) -> None:
    """Clear session state, destroy the session, and confirm the context reset."""
    await _teardown_group(
        deps,
        group,
        chat_jid,
        timestamp,
        reset_context=lambda: clear_durable_context(deps, group),
    )
    logger.info("teardown_trace", step="send_clear_confirmation_start", group=group.name)
    await send_clear_confirmation(deps, chat_jid, source_message=source_message)
    logger.info("teardown_trace", step="send_clear_confirmation_done", group=group.name)


async def handle_scheduled_context_reset(
    deps: ResetSessionDeps,
    task_id: str,
    group: WorkspaceProfile,
    occurrence_id: str,
) -> None:
    """Reset a bound thread once before a scheduled occurrence starts.

    Queue admission already serialized this operation with interactive work.
    Do not stop the queue's current slot or drop later work; no agent process
    for this occurrence exists yet.
    """
    logger.info(
        "scheduled_context_reset",
        task_id=task_id,
        occurrence_id=occurrence_id,
        group=group.name,
    )
    await clear_durable_context(deps, group)
    await send_clear_confirmation(deps, group.jid)


async def handle_end_session(
    deps: SessionDeps,
    chat_jid: str,
    group: WorkspaceProfile,
    timestamp: str,
    *,
    source_message: NewMessage | None = None,
) -> None:
    """Sync worktree and spin down the container without clearing context.

    Unlike context reset, this preserves conversation history. The next
    message will start a fresh container that picks up where it left off.
    """
    await _teardown_group(deps, group, chat_jid, timestamp)
    await _send_command_confirmation(deps, chat_jid, source_message, "👋")


async def send_clear_confirmation(
    deps: SessionDeps,
    chat_jid: str,
    *,
    source_message: NewMessage | None = None,
) -> None:
    """Set cleared_at, store and broadcast a system confirmation."""
    # Mark clear boundary — messages before this are hidden
    cleared_ts = datetime.now(UTC).isoformat()
    completions = await set_chat_cleared_at(chat_jid, cleared_ts)
    deps.emit(ChatClearedEvent(chat_jid=chat_jid))

    try:
        await _send_command_confirmation(deps, chat_jid, source_message, "🗑️")
    finally:
        for completion in completions:
            await notify_conversation_delivery_completed(completion)


async def _send_command_confirmation(
    deps: SessionDeps,
    chat_jid: str,
    source_message: NewMessage | None,
    emoji: str,
) -> None:
    """React to a channel command or retain a visible confirmation for local UIs."""
    is_application_command = source_message is not None and isinstance(
        (source_message.metadata or {}).get("application_command"), dict
    )
    if (
        source_message is not None
        and not is_application_command
        and any(ch.owns_jid(chat_jid) for ch in deps.channels)
    ):
        await send_reaction_to_channels(
            deps, chat_jid, source_message.id, source_message.sender, emoji
        )
        return
    await deps.broadcast_host_message(chat_jid, emoji)


async def trigger_manual_redeploy(
    deps: SessionDeps,
    chat_jid: str,
    *,
    source_message: NewMessage | None = None,
) -> None:
    """Handle a manual redeploy command through Temporal."""
    sha, config_hash = deps.current_deploy_revision()
    logger.info("Manual redeploy triggered via command", chat_jid=chat_jid)
    await _send_command_confirmation(deps, chat_jid, source_message, "🔄")

    await start_deploy_workflow(
        DeployRequest(
            chat_jid=chat_jid,
            commit_sha=sha,
            config_hash=config_hash,
            previous_sha=sha,
            rebuild=False,
            reason="manual_redeploy",
            force=True,
        )
    )


async def ingest_user_message(
    deps: SessionDeps, msg: NewMessage, *, source_channel: str | None = None
) -> None:
    """Unified user message ingestion — stores, emits, and relays user input.

    This is the common code path for user inputs from every channel.

    Args:
        deps: Session dependencies
        msg: The user message to ingest
        source_channel: Optional name of the originating channel. Physical
            channel input retains sender attribution when relayed elsewhere.
    """
    # 1. Store full chat body in SQLite. Phoenix remains an observability trace store.
    metadata = {"source": source_channel or "channel", **(msg.metadata or {})}
    await store_message(
        NewMessage(
            id=msg.id,
            chat_jid=msg.chat_jid,
            sender=msg.sender,
            sender_name=msg.sender_name,
            content=msg.content,
            timestamp=msg.timestamp,
            is_from_me=msg.is_from_me,
            message_type=msg.message_type,
            metadata=metadata,
        ),
        message_type=msg.message_type or "user",
    )

    # 2. Emit to the event bus for observers and logging.
    deps.emit(
        MessageEvent(
            chat_jid=msg.chat_jid,
            sender_name=msg.sender_name,
            content=msg.content,
            timestamp=msg.timestamp,
            is_bot=False,
        )
    )

    # 3. Physical-channel input keeps its origin visible when it reaches a
    # different channel, so it is not mistaken for bot output.
    channel_text = f"[{msg.sender_name}] {msg.content}"
    relay_source = "cross_post"

    # 4. Broadcast to all connected channels except an actual source channel.
    # The source is skipped so magic-word detection there is unaffected — and
    # receiving channels will not re-ingest bot-posted messages (Slack filters
    # bot_id, WhatsApp filters IsFromMe echoes).
    await broadcast(
        deps,
        msg.chat_jid,
        OutboundEvent(type=OutboundEventType.TEXT, content=channel_text),
        skip_channel=source_channel,
        source=relay_source,
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
