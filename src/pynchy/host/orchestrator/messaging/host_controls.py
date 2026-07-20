"""Serialized classification and execution of human control messages."""

from __future__ import annotations

import asyncio

import pynchy.types as types  # noqa: TC001, RUF100 - beartype resolves control annotations.
from pynchy.host.orchestrator.messaging import approval_handler, commands
from pynchy.host.orchestrator.messaging.cursor import advance_cursor
from pynchy.host.orchestrator.messaging.deps import (  # noqa: TC001, RUF100 - beartype resolves control annotations.
    MessageHandlerDeps,
)
from pynchy.host.orchestrator.messaging.direct_command import execute_direct_command
from pynchy.logger import logger
from pynchy.state import mark_message_as_host

_turn_boundary_locks: dict[str, asyncio.Lock] = {}


def turn_boundary_lock(chat_jid: str) -> asyncio.Lock:
    """Serialize active-control classification with turn finalization."""
    return _turn_boundary_locks.setdefault(chat_jid, asyncio.Lock())


async def intercept_special_command(
    deps: MessageHandlerDeps,
    chat_jid: str,
    group: types.WorkspaceProfile,
    message: types.NewMessage,
    *,
    advance_command_cursor: bool = True,
) -> bool:
    """Handle one trusted human control message outside agent context."""
    if (message.metadata or {}).get("authenticated_external_route") is True:
        return False
    content = message.content.strip()
    logger.info("intercept_trace", step="start", group=group.name, content=content[:50])

    if commands.is_context_reset(content):
        logger.info("intercept_trace", step="context_reset_start", group=group.name)
        await deps.handle_context_reset(
            chat_jid,
            group,
            message.timestamp,
            source_message=message,
        )
        logger.info("Context reset", group=group.name)
        return True

    if commands.is_end_session(content):
        logger.info("intercept_trace", step="end_session_start", group=group.name)
        await deps.handle_end_session(
            chat_jid,
            group,
            message.timestamp,
            source_message=message,
        )
        logger.info("End session", group=group.name)
        return True

    if commands.is_redeploy(content):
        await advance_cursor(deps, chat_jid, message.timestamp)
        await deps.trigger_manual_redeploy(chat_jid, source_message=message)
        return True

    if approval := commands.is_approval_command(content):
        action, short_id = approval
        await approval_handler.handle_approval_command(
            deps, chat_jid, action, short_id, message.sender
        )
    elif commands.is_pending_query(content):
        await approval_handler.handle_pending_query(deps, chat_jid)
    elif content.startswith("!") and content[1:]:
        await execute_direct_command(deps, chat_jid, group, message, content[1:])
    else:
        return False

    if advance_command_cursor:
        await advance_cursor(deps, chat_jid, message.timestamp)
    return True


def mark_dispatched(deps: MessageHandlerDeps, chat_jid: str, new_timestamp: str) -> None:
    """Record an in-memory active-container boundary without persisting it."""
    deps.mark_dispatched(chat_jid, new_timestamp)


def host_control_kind(message: types.NewMessage) -> tuple[bool, bool]:
    """Return whether a message is an inline or lifecycle host control."""
    content = message.content.strip()
    inline = bool(
        commands.is_approval_command(content)
        or commands.is_pending_query(content)
        or (content.startswith("!") and content[1:])
    )
    deferred = bool(
        commands.is_context_reset(content)
        or commands.is_end_session(content)
        or commands.is_redeploy(content)
    )
    return inline, deferred


async def reclassify_host_control(
    deps: MessageHandlerDeps,
    chat_jid: str,
    group: types.WorkspaceProfile,
    message: types.NewMessage,
) -> bool:
    """Execute or defer one human control and durably hide it from the agent."""
    inline_control, deferred_control = host_control_kind(message)
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
    group: types.WorkspaceProfile,
    messages: list[types.NewMessage],
    *,
    defer_lifecycle: bool,
) -> int:
    """Consume every inline control while preserving other input order."""
    handled = 0
    for message in messages:
        inline_control, deferred_control = host_control_kind(message)
        if not inline_control and not (deferred_control and defer_lifecycle):
            continue
        if await reclassify_host_control(deps, chat_jid, group, message):
            handled += 1
    return handled


async def execute_deferred_host_controls(
    deps: MessageHandlerDeps,
    chat_jid: str,
    group: types.WorkspaceProfile,
    messages: list[types.NewMessage],
) -> None:
    """Execute lifecycle controls after the associated agent boundary."""
    for message in messages:
        if (message.metadata or {}).get("deferred_host_control") is not True:
            continue
        await intercept_special_command(deps, chat_jid, group, message)


async def should_skip_batch(
    deps: MessageHandlerDeps,
    chat_jid: str,
    group: types.WorkspaceProfile,
    missed_messages: list[types.NewMessage],
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
        await advance_cursor(deps, chat_jid, missed_messages[-1].timestamp)
        return True
    if missed_messages[-1].message_type == "host":
        return False
    return await intercept_special_command(deps, chat_jid, group, missed_messages[-1])
