"""Message routing and polling loop — dispatches incoming messages to agents or tasks.

Decides whether to enqueue a new container run, pipe messages to an
active container, interrupt a running scheduled task, or skip the group
entirely (trigger/access rules, system-notice filtering, special commands).

The processing pipeline itself lives in :mod:`message_handler` — this
module only handles *how* messages arrive and get dispatched.
"""

from __future__ import annotations

import asyncio
import time as _time
from typing import TYPE_CHECKING

from pynchy.chat.commands import is_any_magic_command
from pynchy.chat.message_handler import (
    MessageHandlerDeps,
    _mark_dispatched,
    intercept_special_command,
)
from pynchy.config import get_settings
from pynchy.db import get_messages_since, get_new_messages
from pynchy.logger import logger
from pynchy.utils import create_background_task

if TYPE_CHECKING:
    from collections.abc import Callable

    from pynchy.types import NewMessage, WorkspaceProfile


async def _route_incoming_group(
    deps: MessageHandlerDeps,
    group_jid: str,
    group: WorkspaceProfile,
    group_messages: list[NewMessage],
) -> None:
    """Route newly arrived messages for a single group.

    Decides whether to enqueue a new container run, pipe messages to an
    active container, or interrupt a running scheduled task.  Early-returns
    when the group should be skipped (access/trigger rules, system-notice
    filtering, special commands).
    """
    s = get_settings()
    from pynchy.config_access import is_user_allowed, resolve_allowed_users, resolve_channel_config

    channel_plugin_name = next(
        (ch.name for ch in deps.channels if ch.owns_jid(group_jid)),
        None,
    )

    resolved = resolve_channel_config(
        group.folder,
        channel_jid=group_jid,
        channel_plugin_name=channel_plugin_name,
    )

    # Access check: skip write-only or read-only workspaces
    if resolved.access in ("read", "write"):
        logger.info("route_trace", step="skip_access", group=group.name, access=resolved.access)
        return

    # Sender filter: only process messages from allowed users
    allowed = resolve_allowed_users(
        resolved.allowed_users,
        s.user_groups,
        s.owner,
        channel_plugin_name=channel_plugin_name,
    )
    filtered = []
    for m in group_messages:
        if is_user_allowed(m.sender, channel_plugin_name, allowed, m.is_from_me):
            filtered.append(m)
        else:
            logger.info(
                "route_trace",
                step="skip_sender",
                group=group.name,
                sender=m.sender,
            )
    group_messages = filtered
    if not group_messages:
        logger.info("route_trace", step="skip_all_filtered", group=group.name)
        return

    is_admin_group = group.is_admin
    needs_trigger = not is_admin_group and resolved.trigger == "mention"

    if needs_trigger:
        has_trigger = any(s.trigger_pattern.search(m.content.strip()) for m in group_messages)
        # Magic commands (c, boom, done, r, etc.) bypass trigger
        last_content = group_messages[-1].content.strip()
        if not has_trigger and not is_any_magic_command(last_content):
            logger.info(
                "route_trace",
                step="skip_no_trigger",
                group=group.name,
            )
            return

    # Use the furthest of the processed cursor and the dispatched-but-not-yet-
    # completed cursor.  When a container is active, _dispatched_through is
    # ahead of last_agent_timestamp so follow-up pipes don't re-include the
    # messages the container is already handling.
    cursor = max(
        deps.last_agent_timestamp.get(group_jid, ""),
        deps._dispatched_through.get(group_jid, ""),
    )
    logger.info("route_trace", step="get_messages_since", group=group.name, cursor=cursor[:30] if cursor else "empty")
    all_pending = await get_messages_since(group_jid, cursor)
    if not all_pending:
        logger.info("route_trace", step="skip_no_pending", group=group.name)
        return

    # System notices (e.g. clean rebase notifications) shouldn't wake a
    # sleeping agent — they're just context for the next real session.
    # Skip if *all* pending messages are notices and no container is running.
    if not deps.queue.is_active_task(group_jid) and all(
        m.sender == "system_notice" for m in all_pending
    ):
        return

    logger.info(
        "route_trace",
        step="intercept_check",
        group=group.name,
        last_content=all_pending[-1].content[:50],
    )
    if await intercept_special_command(deps, group_jid, group, all_pending[-1]):
        logger.info("route_trace", step="intercepted", group=group.name)
        return

    logger.info("route_trace", step="not_intercepted", group=group.name)

    formatted = "\n".join(f"{msg.sender_name}: {msg.content}" for msg in all_pending)
    last_content = all_pending[-1].content.strip()
    is_btw = last_content.lower().startswith("btw ")

    # --- Active scheduled task: forward, add todo, or interrupt ---
    if deps.queue.is_active_task(group_jid):
        logger.info("route_trace", step="active_task_forward", group=group.name)
        await _handle_message_during_task(deps, group_jid, group, formatted, last_content, is_btw)
        return

    # --- Active message container: pipe follow-up messages ---
    if deps.queue.send_message(group_jid, formatted):
        logger.info("route_trace", step="piped_to_container", group=group.name)
        if is_btw:
            # Non-interrupting — forward to active container via IPC but
            # don't advance the cursor.  Will be reprocessed after the
            # agent finishes its current turn.
            await deps.broadcast_to_channels(group_jid, f"\u00bb [Forwarded] {last_content[:500]}")
            deps.queue.enqueue_message_check(group_jid)
        else:
            logger.debug(
                "Piped messages to active container",
                chat_jid=group_jid,
                count=len(all_pending),
            )
            last_msg = all_pending[-1]
            await deps.send_reaction_to_channels(group_jid, last_msg.id, last_msg.sender, "🦀")
            _mark_dispatched(deps, group_jid, all_pending[-1].timestamp)
        return

    # --- No active container: enqueue a new run ---
    logger.info("route_trace", step="enqueue_new_run", group=group.name)
    deps.queue.enqueue_message_check(group_jid)


async def _handle_message_during_task(
    deps: MessageHandlerDeps,
    group_jid: str,
    group: WorkspaceProfile,
    formatted: str,
    last_content: str,
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
        deps.queue.send_message(group_jid, formatted)
        await deps.broadcast_to_channels(group_jid, f"\u00bb [Forwarded] {last_content[:500]}")
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
        # or system message injection, this workaround can be replaced.
        from pynchy.todos import add_todo

        item = last_content[5:]  # strip "todo " prefix
        add_todo(group.folder, item)
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
    """Main polling loop — checks for new messages every message_poll interval."""
    s = get_settings()
    _CATCHUP_INTERVAL = 10  # seconds between channel history reconciliation
    _last_catchup = _time.monotonic()

    logger.info(f"🦞 Pynchy running (trigger: @{s.agent.name})")

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
                messages_by_group: dict[str, list] = {}
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

        # Periodically reconcile channel history to recover events
        # dropped by Socket Mode or other transient delivery failures.
        now = _time.monotonic()
        if now - _last_catchup >= _CATCHUP_INTERVAL:
            _last_catchup = now
            try:
                logger.info("message_loop_trace", step="catch_up_start")
                await deps.catch_up_channels()
                logger.info("message_loop_trace", step="catch_up_done")
            except Exception:
                logger.exception("Error in channel catch-up")

        await asyncio.sleep(s.intervals.message_poll)
