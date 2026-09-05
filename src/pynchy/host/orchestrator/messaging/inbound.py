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
    Callable,  # noqa: TC003 - beartype resolves inbound routing annotations at runtime.
)

import pynchy.host.orchestrator.todos as todos
from pynchy.agent_protocol.api import (
    InFlightWorkKind,  # beartype resolves inbound routing annotations at runtime.
)
from pynchy.host.orchestrator.messaging.cursor import advance_cursor
from pynchy.host.orchestrator.messaging.deps import (  # noqa: TC001 - beartype resolves routing annotations.
    MessageHandlerDeps,
)
from pynchy.host.orchestrator.messaging.host_controls import (
    host_control_kind,
    intercept_immediate_checkpoint_controls,
    intercept_special_command,
    mark_dispatched,
    reclassify_batch_host_controls,
    turn_boundary_lock,
)
from pynchy.host.orchestrator.messaging.sender_policy import (
    allowed_group_messages,
    load_allowed_group_messages,
)
from pynchy.identifiers import (
    RuntimeId,  # beartype resolves inbound routing annotations at runtime.
)
from pynchy.logger import logger
from pynchy.plugins.api import (  # beartype resolves inbound routing annotations at runtime.
    NewMessage,
    OutboundEvent,
    OutboundEventType,
)
from pynchy.state.api import (
    get_new_messages,
    get_oldest_resumable_turn_for_group,
    message_cursor,
)
from pynchy.workspace.api import (  # beartype resolves inbound routing annotations at runtime.
    RuntimeTarget,
    WorkspaceProfile,
)


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
    group_messages = allowed_group_messages(deps, group_jid, group, group_messages)
    if not group_messages:
        return

    all_pending = await _pending_messages_for_group(deps, group_jid, group)
    if not all_pending:
        return

    if await _intercept_pending_command(deps, group_jid, group, all_pending):
        return

    await _route_pending_messages(deps, group_jid, group, all_pending)


async def _pending_messages_for_group(
    deps: MessageHandlerDeps,
    group_jid: str,
    group: WorkspaceProfile,
) -> list[NewMessage]:
    cursor = deps.routing_cursor(group_jid)
    logger.info(
        "route_trace",
        step="get_messages_since",
        group=group.name,
        cursor=cursor[:30] if cursor else "empty",
    )
    all_pending = await load_allowed_group_messages(
        deps,
        group_jid,
        group,
        cursor,
    )
    if not all_pending:
        logger.info("route_trace", step="skip_no_pending", group=group.name)
        return []
    if _only_system_notices_while_idle(deps, group, all_pending):
        return []
    return all_pending


def _only_system_notices_while_idle(
    deps: MessageHandlerDeps,
    group: WorkspaceProfile,
    all_pending: list[NewMessage],
) -> bool:
    # System notices (e.g. clean rebase notifications) shouldn't wake a
    # sleeping agent — they're just context for the next real session.
    # Skip if *all* pending messages are notices and no container is running.
    return not deps.queue.is_active_task(RuntimeId(group.folder)) and all(
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
    control_result = await _intercept_host_control_batch(
        deps,
        group_jid,
        group,
        all_pending,
    )
    if control_result is not None:
        return control_result
    last_message = all_pending[-1]
    if await intercept_special_command(deps, group_jid, group, last_message):
        logger.info("route_trace", step="intercepted", group=group.name)
        return True
    logger.info("route_trace", step="not_intercepted", group=group.name)
    return False


async def _intercept_host_control_batch(
    deps: MessageHandlerDeps,
    group_jid: str,
    group: WorkspaceProfile,
    all_pending: list[NewMessage],
) -> bool | None:
    immediate_result = await intercept_immediate_checkpoint_controls(
        deps,
        group_jid,
        group,
        all_pending,
    )
    if immediate_result is not None:
        return immediate_result
    if not any(
        message.message_type != "host" and any(host_control_kind(deps, message))
        for message in all_pending
    ):
        return None
    async with turn_boundary_lock(group_jid):
        # This lock may have waited for the active turn to finalize. Refresh
        # against the now-current cursor so an already-committed routed
        # delivery cannot be forwarded into a duplicate agent turn.
        all_pending[:] = await load_allowed_group_messages(
            deps,
            group_jid,
            group,
            deps.routing_cursor(group_jid),
        )
        if not any(
            message.message_type != "host" and any(host_control_kind(deps, message))
            for message in all_pending
        ):
            return True if not all_pending else None
        active_turn = await get_oldest_resumable_turn_for_group(
            group.folder,
            {InFlightWorkKind.INTERACTIVE},
        )
        defer_lifecycle = (
            active_turn is not None
            or len([message for message in all_pending if message.sender != "system_notice"]) > 1
        )
        handled = await reclassify_batch_host_controls(
            deps,
            group_jid,
            group,
            all_pending,
            defer_lifecycle=defer_lifecycle,
        )
        if not handled:
            return None
        has_deferred = any(
            (message.metadata or {}).get("deferred_host_control") is True for message in all_pending
        )
        remaining_agent_input = any(
            message.message_type != "host" and message.sender != "system_notice"
            for message in all_pending
        )
        intercepted = False
        if active_turn is not None and has_deferred:
            deps.queue.enqueue_message_check(RuntimeTarget.from_binding(group.folder, group_jid))
            logger.info(
                "route_trace",
                step="active_deferred_control_consumed",
                group=group.name,
            )
            intercepted = True
        elif active_turn is not None:
            mark_dispatched(deps, group_jid, message_cursor(all_pending[-1]))
            if not remaining_agent_input:
                logger.info(
                    "route_trace",
                    step="active_inline_control_consumed",
                    group=group.name,
                )
                intercepted = True
        elif not remaining_agent_input:
            await _execute_deferred_controls_without_agent(
                deps,
                group_jid,
                group,
                all_pending,
            )
            intercepted = True
        if not intercepted:
            logger.info(
                "route_trace",
                step="batch_controls_consumed",
                group=group.name,
            )
        return intercepted


async def _execute_deferred_controls_without_agent(
    deps: MessageHandlerDeps,
    group_jid: str,
    group: WorkspaceProfile,
    messages: list[NewMessage],
) -> None:
    for message in messages:
        if (message.metadata or {}).get("deferred_host_control") is True:
            await intercept_special_command(deps, group_jid, group, message)
    await advance_cursor(deps, group_jid, message_cursor(messages[-1]))


async def _route_pending_messages(
    deps: MessageHandlerDeps,
    group_jid: str,
    group: WorkspaceProfile,
    all_pending: list[NewMessage],
) -> None:
    agent_pending = [message for message in all_pending if message.message_type != "host"]
    if not agent_pending:
        return
    runtime_id = RuntimeId(group.folder)
    if deps.queue.is_active_task(runtime_id):
        logger.info("route_trace", step="active_task_forward", group=group.name)
        await _handle_message_during_task(deps, group_jid, group, agent_pending)
        return

    formatted = "\n".join(f"{msg.sender_name}: {msg.content}" for msg in agent_pending)
    if deps.queue.send_message(runtime_id, formatted):
        await _forward_to_active_container(
            deps,
            RuntimeTarget.from_binding(group.folder, group_jid),
            agent_pending,
        )
        return

    # A host runner has no IPC watcher, so send_message deliberately returns
    # False while one is active. Defer the pending input until Codex finishes
    # its current tool; on an idle group this is a harmless no-op.
    deps.queue.defer_interrupt_until_tool_result(runtime_id)
    await _start_new_interactive_turn(deps, group_jid, group, agent_pending[0])


async def _forward_to_active_container(
    deps: MessageHandlerDeps,
    target: RuntimeTarget,
    all_pending: list[NewMessage],
) -> None:
    group_jid = target.chat_jid
    last_content = all_pending[-1].content.strip()
    logger.info("route_trace", step="piped_to_container")
    if last_content.lower().startswith("btw "):
        # Non-interrupting — forward to active container via IPC but
        # don't advance the cursor.  Will be reprocessed after the
        # agent finishes its current turn.
        msg = f"\u00bb [Forwarded] {last_content[:500]}"
        await deps.broadcast_to_channels(
            group_jid, OutboundEvent(type=OutboundEventType.TEXT, content=msg)
        )
        deps.queue.enqueue_message_check(target)
        return

    logger.debug(
        "Piped messages to active container",
        chat_jid=group_jid,
        count=len(all_pending),
    )
    last_msg = all_pending[-1]
    await deps.send_reaction_to_channels(group_jid, last_msg.id, last_msg.sender, "🦀")
    mark_dispatched(deps, group_jid, message_cursor(last_msg))


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
    messages: list[NewMessage],
) -> None:
    """Handle an incoming message when a scheduled task is running.

    "btw" messages are forwarded non-interruptingly via IPC. Todo items use
    the configured canonical board. All other messages interrupt the running
    task.
    """
    last_content = messages[-1].content.strip()
    runtime_id = RuntimeId(group.folder)
    target = RuntimeTarget.from_binding(group.folder, group_jid)
    if last_content.lower().startswith("btw "):
        # Preserve the fast IPC handoff for persistent containers, but also
        # defer a boundary interruption for host execution and long-running
        # queries. The cursor remains unchanged so drain replays the message.
        formatted = "\n".join(f"{msg.sender_name}: {msg.content}" for msg in messages)
        deps.queue.send_message(runtime_id, formatted)
        deps.queue.defer_interrupt_until_tool_result(runtime_id)
        msg = f"\u00bb [Forwarded] {last_content[:500]}"
        await deps.broadcast_to_channels(
            group_jid, OutboundEvent(type=OutboundEventType.TEXT, content=msg)
        )
        deps.queue.enqueue_message_check(target)
    elif last_content.lower().startswith("todo "):
        # Non-interrupting — host writes the workspace's canonical todo board,
        # then notifies the active agent via IPC.
        #
        # Tightly coupled to the Claude SDK: the SDK does not expose
        # APIs to inject true system messages or invoke MCP tools from
        # outside the agent's query loop. Non-Linear workspaces therefore edit
        # todos.json directly (bypassing list_todos / complete_todo), while a
        # Linear workspace writes only its canonical board. Both use a system
        # notice so the agent treats this as informational rather than a request.
        item = last_content[5:]  # strip "todo " prefix
        linear_enabled = deps.linear_workspace_enabled(group)
        issue = await deps.create_linear_workspace_todo(group, item) if linear_enabled else None
        if not linear_enabled:
            todos.add_todo(deps.message_data_dir, group.folder, item)
        if linear_enabled and issue is None:
            await deps.broadcast_to_channels(
                group_jid,
                OutboundEvent(
                    type=OutboundEventType.TEXT,
                    content="⚠️ Pynchy could not create the Linear todo. Please retry.",
                ),
            )
        else:
            # Only confirmed writes become agent context; failed input stays queued.
            board_label = "Linear" if linear_enabled else "your local"
            deps.queue.send_message(
                runtime_id,
                "[System notice \u2014 no response needed] "
                f"User added a todo item to {board_label} list: {item}",
            )
        # Same as "btw ": don't advance cursor, mark pending so drain
        # reprocesses.
        deps.queue.enqueue_message_check(target)
    else:
        # Queue regular messages until the active agent has completed its
        # current tool. Killing immediately can lose a half-finished tool and
        # makes host-mode agents unresponsive because they have no IPC loop.
        deps.queue.clear_pending_tasks(runtime_id)
        deps.queue.defer_interrupt_until_tool_result(runtime_id)
        deps.queue.enqueue_message_check(target)


async def _poll_incoming_messages(deps: MessageHandlerDeps) -> None:
    jids = list(deps.workspaces.keys())
    messages, new_timestamp = await get_new_messages(jids, deps.last_timestamp)

    if messages:
        logger.info("New messages", count=len(messages))

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

        # Commit the batch only after every known group reached its durable
        # routing boundary. A routing failure leaves the batch visible to retry.
        deps.last_timestamp = new_timestamp
        logger.info("message_loop_trace", step="save_state_start")
        await deps.save_state()
        logger.info("message_loop_trace", step="save_state_done")


async def start_message_loop(
    deps: MessageHandlerDeps,
    shutting_down: Callable[[], bool],
) -> None:
    """Main polling loop — checks for incoming messages every message_poll interval."""
    logger.info("🦞 Pynchy running", trigger=deps.agent_name)

    while not shutting_down():
        try:
            await _poll_incoming_messages(deps)
        except Exception:  # noqa: BLE001 - message loop is the routing boundary; keep polling after a failure.
            logger.exception("Error in message loop")

        await asyncio.sleep(deps.message_poll_interval)
