"""Serialized classification and execution of human control messages."""

from __future__ import annotations

import asyncio

from pynchy.agent_protocol.api import CheckpointControlState, InFlightTurn
from pynchy.host.orchestrator.messaging import approval_handler, commands
from pynchy.host.orchestrator.messaging.cursor import advance_cursor, monotonic_cursor
from pynchy.host.orchestrator.messaging.deps import (  # noqa: TC001 - beartype resolves control annotations.
    MessageHandlerDeps,
)
from pynchy.host.orchestrator.messaging.direct_command import execute_direct_command
from pynchy.host.orchestrator.messaging.sender_policy import load_allowed_group_messages
from pynchy.host.orchestrator.scheduled_turn import pause_queued_once_task
from pynchy.identifiers import RuntimeId
from pynchy.logger import logger
from pynchy.plugins.api import (  # noqa: TC001 - beartype resolves control annotations at runtime.
    NewMessage,
)
from pynchy.state.api import (
    clear_in_flight_turn,
    consume_in_flight_control_message,
    finalize_in_flight_pause,
    get_messages_since,
    mark_message_as_host,
    message_cursor,
    message_exists,
    request_in_flight_turn_control,
)
from pynchy.workspace.api import RuntimeTarget, WorkspaceProfile

_turn_boundary_locks: dict[str, asyncio.Lock] = {}


def turn_boundary_lock(chat_jid: str) -> asyncio.Lock:
    """Serialize active-control classification with turn finalization."""
    return _turn_boundary_locks.setdefault(chat_jid, asyncio.Lock())


async def _consume_checkpoint_control(
    deps: MessageHandlerDeps,
    chat_jid: str,
    message: NewMessage,
    requested_state: CheckpointControlState,
) -> InFlightTurn | None:
    """Persist command consumption and its checkpoint transition before stopping work."""
    if await message_exists(message.id, chat_jid):
        turn = await consume_in_flight_control_message(
            message.id,
            chat_jid,
            message_cursor(message),
            deps.last_agent_timestamp,
            requested_state,
        )
        deps.last_agent_timestamp[chat_jid] = monotonic_cursor(
            deps.last_agent_timestamp.get(chat_jid, ""),
            message_cursor(message),
        )
    else:
        # Synthetic callers do not have a persisted channel message. Production
        # intake always takes the atomic branch above.
        turn = await request_in_flight_turn_control(chat_jid, requested_state)
        await advance_cursor(deps, chat_jid, message_cursor(message))
    message.message_type = "host"
    message.metadata = {
        key: value
        for key, value in (message.metadata or {}).items()
        if key != "deferred_host_control"
    }
    return turn


async def _send_pause_confirmation(
    deps: MessageHandlerDeps,
    chat_jid: str,
    message: NewMessage,
) -> None:
    is_application_command = isinstance((message.metadata or {}).get("application_command"), dict)
    if not is_application_command and any(channel.owns_jid(chat_jid) for channel in deps.channels):
        await deps.send_reaction_to_channels(chat_jid, message.id, message.sender, "⏸️")
    else:
        await deps.broadcast_host_message(chat_jid, "⏸️")


async def _handle_pause(
    deps: MessageHandlerDeps,
    chat_jid: str,
    group: WorkspaceProfile,
    message: NewMessage,
) -> None:
    runtime_id = RuntimeId(group.folder)
    had_active_run = deps.queue.has_active_run(runtime_id)
    turn = await _consume_checkpoint_control(
        deps,
        chat_jid,
        message,
        CheckpointControlState.PAUSE_REQUESTED,
    )
    queued_task_ids = deps.queue.clear_pending_tasks(runtime_id)
    for task_id in queued_task_ids:
        await pause_queued_once_task(task_id, group, chat_jid)
    await deps.queue.stop_active_process_for_control(runtime_id)
    await deps.queue.destroy_runtime_session(runtime_id)
    if turn is not None and not had_active_run:
        await finalize_in_flight_pause(turn.turn_id)
    await _send_pause_confirmation(deps, chat_jid, message)


async def _intercept_checkpoint_command(
    deps: MessageHandlerDeps,
    chat_jid: str,
    group: WorkspaceProfile,
    message: NewMessage,
    content: str,
) -> bool:
    if commands.is_pause(deps.command_matcher, content, message.metadata):
        logger.info("intercept_trace", step="pause_start", group=group.name)
        await _handle_pause(deps, chat_jid, group, message)
        logger.info("Agent turn paused", group=group.name)
        return True

    if not commands.is_context_reset(deps.command_matcher, content, message.metadata):
        return False
    logger.info("intercept_trace", step="context_reset_start", group=group.name)
    had_active_run = deps.queue.has_active_run(RuntimeId(group.folder))
    turn = await _consume_checkpoint_control(
        deps,
        chat_jid,
        message,
        CheckpointControlState.RESET_REQUESTED,
    )
    await deps.handle_context_reset(
        chat_jid,
        group,
        message_cursor(message),
        source_message=message,
    )
    if turn is not None and not had_active_run:
        await clear_in_flight_turn(turn.turn_id)
    logger.info("Context reset", group=group.name)
    return True


async def intercept_special_command(
    deps: MessageHandlerDeps,
    chat_jid: str,
    group: WorkspaceProfile,
    message: NewMessage,
    *,
    advance_command_cursor: bool = True,
) -> bool:
    """Handle one trusted human control message outside agent context."""
    if (message.metadata or {}).get("authenticated_external_route") is True:
        return False
    content = message.content.strip()
    logger.info("intercept_trace", step="start", group=group.name, content=content[:50])

    if await _intercept_checkpoint_command(deps, chat_jid, group, message, content):
        return True

    if commands.is_end_session(deps.command_matcher, content, message.metadata):
        logger.info("intercept_trace", step="end_session_start", group=group.name)
        await deps.handle_end_session(
            chat_jid,
            group,
            message_cursor(message),
            source_message=message,
        )
        logger.info("End session", group=group.name)
        return True

    if commands.is_redeploy(deps.command_matcher, content, message.metadata):
        await advance_cursor(deps, chat_jid, message_cursor(message))
        await deps.trigger_manual_redeploy(chat_jid, source_message=message)
        return True

    if approval := commands.is_approval_command(deps.command_matcher, content, message.metadata):
        action, short_id = approval
        await approval_handler.handle_approval_command(
            deps, chat_jid, action, short_id, message.sender
        )
    elif commands.is_pending_query(deps.command_matcher, content, message.metadata):
        await approval_handler.handle_pending_query(deps, chat_jid)
    elif content.startswith("!") and content[1:]:
        await execute_direct_command(deps, chat_jid, group, message, content[1:])
    else:
        return False

    if advance_command_cursor:
        await advance_cursor(deps, chat_jid, message_cursor(message))
    return True


async def intercept_immediate_checkpoint_controls(
    deps: MessageHandlerDeps,
    chat_jid: str,
    group: WorkspaceProfile,
    pending: list[NewMessage],
) -> bool | None:
    """Execute pause/reset controls before forwarding any active-turn input."""
    if not any(
        message.message_type != "host"
        and (
            commands.is_pause(deps.command_matcher, message.content, message.metadata)
            or commands.is_context_reset(deps.command_matcher, message.content, message.metadata)
        )
        for message in pending
    ):
        return None

    async with turn_boundary_lock(chat_jid):
        pending[:] = await load_allowed_group_messages(
            deps,
            chat_jid,
            group,
            deps.routing_cursor(chat_jid),
            get_messages_since,
        )
        handled = False
        for message in pending:
            if message.message_type == "host":
                continue
            if not (
                commands.is_pause(deps.command_matcher, message.content, message.metadata)
                or commands.is_context_reset(
                    deps.command_matcher, message.content, message.metadata
                )
            ):
                continue
            if await intercept_special_command(deps, chat_jid, group, message):
                handled = True

    if not handled:
        return None
    if any(
        message.message_type != "host" and message.sender != "system_notice" for message in pending
    ):
        # Drain after the stopping queue coroutine releases; forwarding here
        # can write into dead container IPC.
        deps.queue.enqueue_message_check(RuntimeTarget.from_binding(group.folder, chat_jid))
    return True


def mark_dispatched(deps: MessageHandlerDeps, chat_jid: str, new_timestamp: str) -> None:
    """Record an in-memory active-container boundary without persisting it."""
    deps.mark_dispatched(chat_jid, new_timestamp)


def host_control_kind(deps: MessageHandlerDeps, message: NewMessage) -> tuple[bool, bool]:
    """Return whether a message is an inline or lifecycle host control."""
    content = message.content.strip()
    inline = bool(
        commands.is_approval_command(deps.command_matcher, content, message.metadata)
        or commands.is_pending_query(deps.command_matcher, content, message.metadata)
        or (content.startswith("!") and content[1:])
    )
    deferred = bool(
        commands.is_pause(deps.command_matcher, content, message.metadata)
        or commands.is_context_reset(deps.command_matcher, content, message.metadata)
        or commands.is_end_session(deps.command_matcher, content, message.metadata)
        or commands.is_redeploy(deps.command_matcher, content, message.metadata)
    )
    return inline, deferred


async def reclassify_host_control(
    deps: MessageHandlerDeps,
    chat_jid: str,
    group: WorkspaceProfile,
    message: NewMessage,
) -> bool:
    """Execute or defer one human control and durably hide it from the agent."""
    inline_control, deferred_control = host_control_kind(deps, message)
    if (
        not (inline_control or deferred_control)
        or (message.metadata or {}).get("authenticated_external_route") is True
    ):
        return False
    if message.message_type == "host":
        return True
    handled = deferred_control or await intercept_special_command(
        deps,
        chat_jid,
        group,
        message,
        advance_command_cursor=False,
    )
    if not handled:
        return False
    await mark_message_as_host(
        message.id,
        chat_jid,
        deferred_control=deferred_control,
    )
    message.message_type = "host"
    if deferred_control:
        message.metadata = {
            **(message.metadata or {}),
            "deferred_host_control": True,
        }
    return True


async def reclassify_batch_host_controls(
    deps: MessageHandlerDeps,
    chat_jid: str,
    group: WorkspaceProfile,
    messages: list[NewMessage],
    *,
    defer_lifecycle: bool,
) -> int:
    """Consume every inline control while preserving other input order."""
    handled = 0
    for message in messages:
        inline_control, deferred_control = host_control_kind(deps, message)
        if commands.is_pause(
            deps.command_matcher, message.content, message.metadata
        ) or commands.is_context_reset(deps.command_matcher, message.content, message.metadata):
            continue
        if not inline_control and not (deferred_control and defer_lifecycle):
            continue
        if await reclassify_host_control(deps, chat_jid, group, message):
            handled += 1
    return handled


async def execute_deferred_host_controls(
    deps: MessageHandlerDeps,
    chat_jid: str,
    group: WorkspaceProfile,
    messages: list[NewMessage],
) -> None:
    """Execute lifecycle controls after the associated agent boundary."""
    for message in messages:
        if (message.metadata or {}).get("deferred_host_control") is not True:
            continue
        await intercept_special_command(deps, chat_jid, group, message)


async def should_skip_batch(
    deps: MessageHandlerDeps,
    chat_jid: str,
    group: WorkspaceProfile,
    missed_messages: list[NewMessage],
) -> bool:
    """Return whether a batch needs no agent activation."""
    if not missed_messages:
        return True
    if all(message.sender == "system_notice" for message in missed_messages):
        return True

    async with turn_boundary_lock(chat_jid):
        await reclassify_batch_host_controls(
            deps,
            chat_jid,
            group,
            missed_messages,
            defer_lifecycle=len(
                [message for message in missed_messages if message.sender != "system_notice"]
            )
            > 1,
        )

    if all(
        message.message_type == "host" or message.sender == "system_notice"
        for message in missed_messages
    ):
        await execute_deferred_host_controls(deps, chat_jid, group, missed_messages)
        await advance_cursor(deps, chat_jid, message_cursor(missed_messages[-1]))
        return True
    if missed_messages[-1].message_type == "host":
        return False
    return await intercept_special_command(deps, chat_jid, group, missed_messages[-1])
