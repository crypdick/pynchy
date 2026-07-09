"""Message routing and polling loop — dispatches incoming messages to agents or tasks.

Decides whether to enqueue a container run, pipe messages to an
active container, interrupt a running scheduled task, or skip the group
entirely (channel filtering, system-notice filtering, special commands).

The processing pipeline itself lives in :mod:`message_handler` — this
module only handles *how* messages arrive and get dispatched.
"""

from __future__ import annotations

import asyncio
from collections.abc import (
    Callable,  # noqa: TC003, RUF100 - beartype resolves inbound routing annotations at runtime.
)

from pynchy.config import get_settings
from pynchy.host.orchestrator.messaging.pipeline import (
    MessageHandlerDeps,
    _mark_dispatched,
    intercept_special_command,
)
from pynchy.logger import logger
from pynchy.state import get_messages_since, get_new_messages
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves inbound routing annotations at runtime.
    NewMessage,
    WorkspaceProfile,
)
from pynchy.utils import create_background_task


async def _route_incoming_group(
    deps: MessageHandlerDeps,
    group_jid: str,
    group: WorkspaceProfile,
    group_messages: list[NewMessage],
) -> None:
    """Route newly arrived messages for a single group.

    Decides whether to enqueue a container run, pipe messages to an
    active container, or interrupt a running scheduled task.  Early-returns
    when the group should be skipped (channel filtering, system-notice
    filtering, special commands).
    """
    group_messages = _allowed_group_messages(deps, group_jid, group, group_messages)
    if not group_messages:
        return

    all_pending = await _pending_messages_for_group(deps, group_jid, group)
    if not all_pending:
        return

    if await _intercept_pending_command(deps, group_jid, group, all_pending):
        return

    await _route_pending_messages(deps, group_jid, group, group_messages, all_pending)


def _channel_plugin_name(deps: MessageHandlerDeps, group_jid: str) -> str | None:
    return next((ch.name for ch in deps.channels if ch.owns_jid(group_jid)), None)


def _allowed_group_messages(
    deps: MessageHandlerDeps,
    group_jid: str,
    group: WorkspaceProfile,
    group_messages: list[NewMessage],
) -> list[NewMessage]:
    from pynchy.config.access import filter_allowed_messages

    channel_plugin_name = _channel_plugin_name(deps, group_jid)
    filtered_messages = filter_allowed_messages(group_messages, group, channel_plugin_name)
    if not filtered_messages:
        logger.info("route_trace", step="skip_all_filtered", group=group.name)
        return []
    return filtered_messages


def _routing_cursor(deps: MessageHandlerDeps, group_jid: str) -> str:
    # Use the furthest of the processed cursor and the dispatched-but-not-yet-
    # completed cursor.  When a container is active, _dispatched_through is
    # ahead of last_agent_timestamp so follow-up pipes don't re-include the
    # messages the container is already handling.
    return max(
        deps.last_agent_timestamp.get(group_jid, ""),
        deps._dispatched_through.get(group_jid, ""),
    )


async def _pending_messages_for_group(
    deps: MessageHandlerDeps,
    group_jid: str,
    group: WorkspaceProfile,
) -> list[NewMessage]:
    cursor = _routing_cursor(deps, group_jid)
    logger.info(
        "route_trace",
        step="get_messages_since",
        group=group.name,
        cursor=cursor[:30] if cursor else "empty",
    )
    all_pending = await get_messages_since(group_jid, cursor)
    if not all_pending:
        logger.info("route_trace", step="skip_no_pending", group=group.name)
        return []
    if _only_system_notices_while_idle(deps, group_jid, all_pending):
        return []
    return all_pending


def _only_system_notices_while_idle(
    deps: MessageHandlerDeps,
    group_jid: str,
    all_pending: list[NewMessage],
) -> bool:
    # System notices (e.g. clean rebase notifications) shouldn't wake a
    # sleeping agent — they're just context for the next real session.
    # Skip if *all* pending messages are notices and no container is running.
    return not deps.queue.is_active_task(group_jid) and all(
        m.sender == "system_notice" for m in all_pending
    )


async def _intercept_pending_command(
    deps: MessageHandlerDeps,
    group_jid: str,
    group: WorkspaceProfile,
    all_pending: list[NewMessage],
) -> bool:
    logger.info(
        "route_trace",
        step="intercept_check",
        group=group.name,
        last_content=all_pending[-1].content[:50],
    )
    if await intercept_special_command(deps, group_jid, group, all_pending[-1]):
        logger.info("route_trace", step="intercepted", group=group.name)
        return True
    logger.info("route_trace", step="not_intercepted", group=group.name)
    return False


async def _route_pending_messages(
    deps: MessageHandlerDeps,
    group_jid: str,
    group: WorkspaceProfile,
    group_messages: list[NewMessage],
    all_pending: list[NewMessage],
) -> None:
    formatted = "\n".join(f"{msg.sender_name}: {msg.content}" for msg in all_pending)
    last_content = all_pending[-1].content.strip()
    is_btw = last_content.lower().startswith("btw ")

    if deps.queue.is_active_task(group_jid):
        logger.info("route_trace", step="active_task_forward", group=group.name)
        await _handle_message_during_task(
            deps, group_jid, group, formatted, last_content, is_btw=is_btw
        )
        return

    if deps.queue.send_message(group_jid, formatted):
        await _forward_to_active_container(
            deps,
            group_jid,
            all_pending,
            last_content=last_content,
            is_btw=is_btw,
        )
        return

    await _start_new_interactive_turn(deps, group_jid, group, group_messages[0])


async def _forward_to_active_container(
    deps: MessageHandlerDeps,
    group_jid: str,
    all_pending: list[NewMessage],
    *,
    last_content: str,
    is_btw: bool,
) -> None:
    logger.info("route_trace", step="piped_to_container")
    if is_btw:
        # Non-interrupting — forward to active container via IPC but
        # don't advance the cursor.  Will be reprocessed after the
        # agent finishes its current turn.
        from pynchy.types import OutboundEvent, OutboundEventType

        msg = f"\u00bb [Forwarded] {last_content[:500]}"
        await deps.broadcast_to_channels(
            group_jid, OutboundEvent(type=OutboundEventType.TEXT, content=msg)
        )
        deps.queue.enqueue_message_check(group_jid)
        return

    logger.debug(
        "Piped messages to active container",
        chat_jid=group_jid,
        count=len(all_pending),
    )
    last_msg = all_pending[-1]
    await deps.send_reaction_to_channels(group_jid, last_msg.id, last_msg.sender, "🦀")
    _mark_dispatched(deps, group_jid, last_msg.timestamp)


async def _start_new_interactive_turn(
    deps: MessageHandlerDeps,
    group_jid: str,
    group: WorkspaceProfile,
    first_msg: NewMessage,
) -> None:
    logger.info("route_trace", step="enqueue_new_run", group=group.name)
    await deps.send_reaction_to_channels(group_jid, first_msg.id, first_msg.sender, "sunrise")
    await deps.start_interactive_turn(group_jid)


async def _handle_message_during_task(
    deps: MessageHandlerDeps,
    group_jid: str,
    group: WorkspaceProfile,
    formatted: str,
    last_content: str,
    *,
    is_btw: bool,
) -> None:
    """Handle an incoming message when a scheduled task is running.

    "btw" messages are forwarded non-interruptingly via IPC.  Todo items
    are written directly to the group's todo list.  All other messages
    interrupt the running task.
    """
    if is_btw:
        # Non-interrupting — best-effort forward to the running container
        # via IPC.  The cursor is NOT advanced: the container may never
        # read the IPC file (e.g. the agent calls finished_work() before
        # reaching wait_for_ipc_message).  We mark pending_messages so
        # _drain_group reprocesses them after the task exits.
        from pynchy.types import OutboundEvent, OutboundEventType

        deps.queue.send_message(group_jid, formatted)
        msg = f"\u00bb [Forwarded] {last_content[:500]}"
        await deps.broadcast_to_channels(
            group_jid, OutboundEvent(type=OutboundEventType.TEXT, content=msg)
        )
        deps.queue.enqueue_message_check(group_jid)
    elif last_content.lower().startswith("todo "):
        # Non-interrupting — host writes directly to todos.json, then
        # notifies agent via IPC.
        #
        # Tightly coupled to the Claude SDK: the SDK does not expose
        # APIs to inject true system messages or invoke MCP tools from
        # outside the agent's query loop.  So we edit todos.json
        # directly (bypassing the list_todos / complete_todo MCP tools)
        # and use a "[System notice]" prefix convention on the IPC
        # notification so the agent treats it as informational rather
        # than a user request.  If the SDK adds external tool invocation
        # or system message injection, this workaround becomes unnecessary.
        from pynchy.host.orchestrator.todos import add_todo
        from pynchy.plugins.integrations.linear_boot import create_linear_workspace_todo

        item = last_content[5:]  # strip "todo " prefix
        add_todo(group.folder, item)
        await create_linear_workspace_todo(group, item)
        deps.queue.send_message(
            group_jid,
            "[System notice \u2014 no response needed] "
            f"User added a todo item to your list: {item}",
        )
        # Same as "btw ": don't advance cursor, mark pending so drain
        # reprocesses.
        deps.queue.enqueue_message_check(group_jid)
    else:
        # Interrupting — kill the task, process messages after it dies.
        deps.queue.clear_pending_tasks(group_jid)
        deps.queue.enqueue_message_check(group_jid)
        create_background_task(
            deps.queue.stop_active_process(group_jid),
            name=f"interrupt-stop-{group_jid[:20]}",
        )


async def start_message_loop(
    deps: MessageHandlerDeps,
    shutting_down: Callable[[], bool],
) -> None:
    """Main polling loop — checks for incoming messages every message_poll interval."""
    s = get_settings()
    logger.info("🦞 Pynchy running", trigger=s.agent.name)

    while not shutting_down():
        try:
            jids = list(deps.workspaces.keys())
            messages, new_timestamp = await get_new_messages(jids, deps.last_timestamp)

            if messages:
                logger.info("New messages", count=len(messages))

                # Advance "seen" cursor immediately
                deps.last_timestamp = new_timestamp
                logger.info("message_loop_trace", step="save_state_start")
                await deps.save_state()
                logger.info("message_loop_trace", step="save_state_done")

                # Group by chat JID and route each group independently
                messages_by_group: dict[str, list[NewMessage]] = {}
                for msg in messages:
                    messages_by_group.setdefault(msg.chat_jid, []).append(msg)

                for group_jid, group_messages in messages_by_group.items():
                    group = deps.workspaces.get(group_jid)
                    if group:
                        logger.info(
                            "message_loop_trace",
                            step="route_start",
                            group=group.name,
                        )
                        await _route_incoming_group(deps, group_jid, group, group_messages)
                        logger.info(
                            "message_loop_trace",
                            step="route_done",
                            group=group.name,
                        )

        except Exception:
            logger.exception("Error in message loop")

        await asyncio.sleep(s.intervals.message_poll)
