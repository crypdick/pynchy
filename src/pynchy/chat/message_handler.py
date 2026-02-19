"""Message processing pipeline — intercepts commands and routes messages to agents.

Extracted from app.py to keep the orchestrator focused on wiring.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pynchy.chat.commands import (
    is_any_magic_command,
    is_context_reset,
    is_end_session,
    is_redeploy,
)
from pynchy.config import get_settings
from pynchy.db import get_messages_since, get_new_messages, store_message_direct
from pynchy.event_bus import AgentActivityEvent, MessageEvent
from pynchy.git_ops.utils import is_repo_dirty
from pynchy.logger import logger
from pynchy.utils import IdleTimer, create_background_task, generate_message_id

if TYPE_CHECKING:
    from pynchy.group_queue import GroupQueue
    from pynchy.types import ContainerOutput, NewMessage, WorkspaceProfile


class MessageHandlerDeps(Protocol):
    """Dependencies for message processing."""

    @property
    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    @property
    def last_agent_timestamp(self) -> dict[str, str]: ...

    # The "seen" cursor for the polling loop (distinct from per-group agent cursors)
    last_timestamp: str

    @property
    def queue(self) -> GroupQueue: ...

    async def save_state(self) -> None: ...

    async def handle_context_reset(
        self, chat_jid: str, group: WorkspaceProfile, timestamp: str
    ) -> None: ...

    async def handle_end_session(
        self, chat_jid: str, group: WorkspaceProfile, timestamp: str
    ) -> None: ...

    async def trigger_manual_redeploy(self, chat_jid: str) -> None: ...

    async def broadcast_to_channels(
        self, chat_jid: str, text: str, *, suppress_errors: bool = True
    ) -> None: ...

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...

    async def send_reaction_to_channels(
        self, chat_jid: str, message_id: str, sender: str, emoji: str
    ) -> None: ...

    async def set_typing_on_channels(self, chat_jid: str, is_typing: bool) -> None: ...

    async def catch_up_channels(self) -> None: ...

    def emit(self, event: Any) -> None: ...

    async def run_agent(
        self,
        group: WorkspaceProfile,
        chat_jid: str,
        messages: list[dict],
        on_output: Any | None = None,
        extra_system_notices: list[str] | None = None,
        *,
        input_source: str = "user",
    ) -> str: ...

    async def handle_streamed_output(
        self, chat_jid: str, group: WorkspaceProfile, result: ContainerOutput
    ) -> bool: ...


async def intercept_special_command(
    deps: MessageHandlerDeps,
    chat_jid: str,
    group: WorkspaceProfile,
    message: NewMessage,
) -> bool:
    """Check for and handle special commands (reset, end session, redeploy, !cmd).

    Returns True if a command was intercepted and handled, False otherwise.
    """
    content = message.content.strip()

    if is_context_reset(content):
        await deps.handle_context_reset(chat_jid, group, message.timestamp)
        logger.info("Context reset", group=group.name)
        return True

    if is_end_session(content):
        await deps.handle_end_session(chat_jid, group, message.timestamp)
        logger.info("End session", group=group.name)
        return True

    if is_redeploy(content):
        deps.last_agent_timestamp[chat_jid] = message.timestamp
        await deps.save_state()
        await deps.trigger_manual_redeploy(chat_jid)
        return True

    if content.startswith("!"):
        command = content[1:]
        if command:
            await execute_direct_command(deps, chat_jid, group, message, command)
            deps.last_agent_timestamp[chat_jid] = message.timestamp
            await deps.save_state()
            return True

    return False


async def execute_direct_command(
    deps: MessageHandlerDeps,
    chat_jid: str,
    group: WorkspaceProfile,
    message: NewMessage,
    command: str,
) -> None:
    """Execute a user command directly without LLM approval."""
    s = get_settings()
    logger.info("Executing direct command", group=group.name, command=command[:100])

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(s.groups_dir / group.folder),
        )

        if result.returncode == 0:
            output = result.stdout if result.stdout else "(no output)"
            status_emoji = "✅"
        else:
            output = result.stderr if result.stderr else result.stdout or "(no output)"
            status_emoji = "❌"

        ts = datetime.now(UTC).isoformat()
        output_text = (
            f"{status_emoji} Command output (exit {result.returncode}):\n```\n{output}\n```"
        )

        await store_message_direct(
            id=generate_message_id("cmd"),
            chat_jid=chat_jid,
            sender="command_output",
            sender_name="command",
            content=output_text,
            timestamp=ts,
            is_from_me=True,
            message_type="tool_result",
            metadata={"exit_code": result.returncode},
        )

        channel_text = f"🔧 {output_text}"
        await deps.broadcast_to_channels(chat_jid, channel_text)

        deps.emit(
            MessageEvent(
                chat_jid=chat_jid,
                sender_name="command",
                content=output_text,
                timestamp=ts,
                is_bot=True,
            )
        )

        logger.info(
            "Direct command executed",
            group=group.name,
            exit_code=result.returncode,
            output_len=len(output),
        )

    except subprocess.TimeoutExpired:
        error_msg = "⏱️ Command timed out (30s limit)"
        await deps.broadcast_host_message(chat_jid, error_msg)
        logger.warning("Direct command timeout", group=group.name, command=command[:100])
    except Exception as exc:
        error_msg = f"❌ Command failed: {str(exc)}"
        await deps.broadcast_host_message(chat_jid, error_msg)
        logger.error("Direct command error", group=group.name, error=str(exc))


async def _handle_reset_handoff(
    deps: MessageHandlerDeps,
    chat_jid: str,
    group: WorkspaceProfile,
    reset_file: Path,
) -> bool | None:
    """Consume a reset_prompt.json file and run the handoff agent.

    Returns True/False if the reset was handled (success/failure),
    or None if there was no reset to process.
    """
    if not reset_file.exists():
        return None

    s = get_settings()
    try:
        reset_data = json.loads(reset_file.read_text())
        reset_file.unlink()
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "Failed to read reset prompt file",
            group=group.name,
            path=str(reset_file),
            err=str(exc),
        )
        reset_file.unlink(missing_ok=True)
        return True

    reset_message = reset_data.get("message", "")
    if not reset_message:
        return True

    logger.info("Processing reset handoff", group=group.name)

    async def handoff_on_output(result: ContainerOutput) -> None:
        await deps.handle_streamed_output(chat_jid, group, result)

    reset_messages = [
        {
            "message_type": "user",
            "sender": "system",
            "sender_name": "System",
            "content": reset_message,
            "timestamp": datetime.now(UTC).isoformat(),
            "metadata": {"source": "reset_handoff"},
        }
    ]

    result = await deps.run_agent(
        group, chat_jid, reset_messages, handoff_on_output, input_source="reset_handoff"
    )

    if reset_data.get("needsDirtyRepoCheck"):
        dirty_check_file = s.data_dir / "ipc" / group.folder / "needs_dirty_check.json"
        dirty_check_file.write_text(json.dumps({"timestamp": datetime.now(UTC).isoformat()}))

    return result != "error"


def _check_dirty_repo(group_name: str, dirty_check_file: Path) -> list[str]:
    """Check for uncommitted changes and return system notices if dirty.

    Consumes the dirty_check_file marker. Returns a list of system notice
    strings (empty if repo is clean or file doesn't exist).
    """
    notices: list[str] = []
    if not dirty_check_file.exists():
        return notices
    try:
        dirty_check_file.unlink()
        if is_repo_dirty():
            notices.append(
                "WARNING: Uncommitted changes detected in the repository. "
                "Please review and commit these changes so that you may work "
                "with a clean slate. "
                "Run `git status` and `git diff` to see what has changed."
            )
            logger.info("Added dirty repo warning after reset", group=group_name)
    except Exception as exc:
        logger.error("Error checking for dirty repo after reset", err=str(exc))
        dirty_check_file.unlink(missing_ok=True)
    return notices


async def _advance_cursor(deps: MessageHandlerDeps, chat_jid: str, new_timestamp: str) -> str:
    """Advance the agent cursor to *new_timestamp*, persisting to DB.

    Returns the **previous** cursor value so the caller can roll back on
    error.  If ``save_state`` fails the cursor is automatically restored.
    """
    previous = deps.last_agent_timestamp.get(chat_jid, "")
    deps.last_agent_timestamp[chat_jid] = new_timestamp
    try:
        await deps.save_state()
    except Exception:
        deps.last_agent_timestamp[chat_jid] = previous
        raise
    return previous


async def process_group_messages(
    deps: MessageHandlerDeps,
    chat_jid: str,
) -> bool:
    """Process all pending messages for a group. Called by GroupQueue."""
    s = get_settings()
    group = deps.workspaces.get(chat_jid)
    if not group:
        return True

    # Check for agent-initiated context reset prompt
    reset_file = s.data_dir / "ipc" / group.folder / "reset_prompt.json"
    reset_result = await _handle_reset_handoff(deps, chat_jid, group, reset_file)
    if reset_result is not None:
        return reset_result

    is_admin_group = group.is_admin
    since_timestamp = deps.last_agent_timestamp.get(chat_jid, "")
    missed_messages = await get_messages_since(chat_jid, since_timestamp)

    if not missed_messages:
        return True

    # System notices alone shouldn't launch a container — they're context
    # for the next real session, not actionable messages.
    if all(m.sender == "system_notice" for m in missed_messages):
        return True

    # Intercept special commands before trigger check — magic commands
    # (context reset, end session, redeploy) should work without a trigger
    if await intercept_special_command(deps, chat_jid, group, missed_messages[-1]):
        return True

    # For non-admin groups, check if trigger is required and present
    from pynchy.config import resolve_channel_config

    resolved = resolve_channel_config(group.folder)
    if not is_admin_group and resolved.trigger == "mention":
        has_trigger = any(s.trigger_pattern.search(m.content.strip()) for m in missed_messages)
        if not has_trigger:
            return True

    # Access check: if workspace-level access is "read", skip activation (still stored)
    if resolved.access == "read":
        return True

    from pynchy.chat.router import format_messages_for_sdk

    messages = format_messages_for_sdk(missed_messages)

    # Check if we need to add dirty repo warning after context reset
    dirty_check_file = s.data_dir / "ipc" / group.folder / "needs_dirty_check.json"
    reset_system_notices = _check_dirty_repo(group.name, dirty_check_file) if is_admin_group else []

    # Advance cursor with automatic rollback on failure
    previous_cursor = await _advance_cursor(deps, chat_jid, missed_messages[-1].timestamp)

    logger.info(
        "Processing messages",
        group=group.name,
        message_count=len(missed_messages),
        preview=missed_messages[-1].content[:200],
    )

    # Track idle timer for closing stdin when agent is idle
    idle_timer = IdleTimer(s.idle_timeout, lambda: deps.queue.close_stdin(chat_jid))

    # Send emoji reaction on the last message to indicate agent is reading
    last_msg = missed_messages[-1]
    await deps.send_reaction_to_channels(chat_jid, last_msg.id, last_msg.sender, "👀")

    # Set typing indicator on all channels that support it
    await deps.set_typing_on_channels(chat_jid, True)

    deps.emit(AgentActivityEvent(chat_jid=chat_jid, active=True))

    had_error = False
    output_sent_to_user = False

    async def on_output(result: ContainerOutput) -> None:
        nonlocal had_error, output_sent_to_user

        sent = await deps.handle_streamed_output(chat_jid, group, result)
        if sent:
            output_sent_to_user = True
        if result.type == "result":
            idle_timer.reset()
        if result.status == "error":
            had_error = True

    agent_result = await deps.run_agent(
        group, chat_jid, messages, on_output, reset_system_notices or None
    )

    await deps.set_typing_on_channels(chat_jid, False)
    deps.emit(AgentActivityEvent(chat_jid=chat_jid, active=False))
    idle_timer.cancel()

    if agent_result == "error" or had_error:
        if output_sent_to_user:
            logger.warning(
                "Agent error after output was sent, skipping cursor rollback",
                group=group.name,
            )
            return True
        await deps.broadcast_host_message(
            chat_jid, "⚠️ Agent error occurred. Will retry on next message."
        )
        deps.last_agent_timestamp[chat_jid] = previous_cursor
        await deps.save_state()
        logger.warning(
            "Agent error, rolled back message cursor for retry",
            group=group.name,
        )
        return False

    # Merge worktree commits into main and push for groups with repo_access
    from pynchy.git_ops.worktree import background_merge_worktree

    background_merge_worktree(group)

    return True


async def start_message_loop(
    deps: MessageHandlerDeps,
    shutting_down: Callable[[], bool],
) -> None:
    """Main polling loop — checks for new messages every message_poll interval."""
    import time as _time

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
                await deps.save_state()

                # Group by chat JID
                messages_by_group: dict[str, list] = {}
                for msg in messages:
                    messages_by_group.setdefault(msg.chat_jid, []).append(msg)

                for group_jid, group_messages in messages_by_group.items():
                    group = deps.workspaces.get(group_jid)
                    if not group:
                        continue

                    is_admin_group = group.is_admin
                    from pynchy.config import resolve_channel_config

                    loop_resolved = resolve_channel_config(group.folder)

                    # Access check: skip write-only or read-only workspaces
                    if loop_resolved.access in ("read", "write"):
                        continue

                    needs_trigger = not is_admin_group and loop_resolved.trigger == "mention"

                    if needs_trigger:
                        last_content = group_messages[-1].content.strip()
                        has_trigger = any(
                            s.trigger_pattern.search(m.content.strip()) for m in group_messages
                        )
                        # Magic commands (c, boom, done, r, etc.) bypass trigger
                        if not has_trigger and not is_any_magic_command(last_content):
                            logger.debug(
                                "Skipping group, no trigger mention found",
                                group=group.name,
                                group_jid=group_jid,
                            )
                            continue

                    all_pending = await get_messages_since(
                        group_jid,
                        deps.last_agent_timestamp.get(group_jid, ""),
                    )
                    if not all_pending:
                        continue

                    # System notices (e.g. clean rebase notifications) shouldn't
                    # wake a sleeping agent — they're just context for the next
                    # real session.  Skip if *all* pending messages are notices
                    # and no container is already running for this group.
                    if not deps.queue.is_active_task(group_jid) and all(
                        m.sender == "system_notice" for m in all_pending
                    ):
                        continue

                    if await intercept_special_command(deps, group_jid, group, all_pending[-1]):
                        continue

                    formatted = "\n".join(
                        f"{msg.sender_name}: {msg.content}" for msg in all_pending
                    )

                    last_content = all_pending[-1].content.strip()
                    is_btw = last_content.lower().startswith("btw ")

                    if deps.queue.is_active_task(group_jid):
                        if is_btw:
                            # Non-interrupting — best-effort forward to
                            # the running container via IPC.  The cursor
                            # is NOT advanced: the container may never
                            # read the IPC file (e.g. the agent calls
                            # finished_work() during its query before
                            # reaching wait_for_ipc_message).  Instead
                            # we mark pending_messages so _drain_group
                            # reprocesses them after the task exits.
                            deps.queue.send_message(group_jid, formatted)
                            await deps.broadcast_to_channels(
                                group_jid, f"\u00bb [Forwarded] {last_content[:500]}"
                            )
                            deps.queue.enqueue_message_check(group_jid)
                        elif last_content.lower().startswith("todo "):
                            # Non-interrupting — host writes directly to
                            # todos.json, then notifies agent via IPC.
                            #
                            # Tightly coupled to the Claude SDK: the SDK
                            # does not expose APIs to inject true system
                            # messages or invoke MCP tools from outside
                            # the agent's query loop.  So we edit
                            # todos.json directly (bypassing the
                            # list_todos / complete_todo MCP tools) and
                            # use a "[System notice]" prefix convention
                            # on the IPC notification so the agent treats
                            # it as informational rather than a user
                            # request.  If the SDK adds external tool
                            # invocation or system message injection,
                            # this workaround can be replaced.
                            from pynchy.todos import add_todo

                            item = last_content[5:]  # strip "todo " prefix
                            add_todo(group.folder, item)
                            deps.queue.send_message(
                                group_jid,
                                "[System notice \u2014 no response needed] "
                                f"User added a todo item to your list: {item}",
                            )
                            # Same as "btw ": don't advance cursor,
                            # mark pending so drain reprocesses.
                            deps.queue.enqueue_message_check(group_jid)
                        else:
                            # Interrupting — kill the task, process
                            # messages after it dies.
                            deps.queue.clear_pending_tasks(group_jid)
                            deps.queue.enqueue_message_check(group_jid)
                            create_background_task(
                                deps.queue.stop_active_process(group_jid),
                                name=f"interrupt-stop-{group_jid[:20]}",
                            )
                        continue

                    if deps.queue.send_message(group_jid, formatted):
                        if is_btw:
                            # Non-interrupting — forward to active
                            # container via IPC but don't advance the
                            # cursor.  The message will be reprocessed
                            # after the agent finishes its current turn.
                            await deps.broadcast_to_channels(
                                group_jid, f"\u00bb [Forwarded] {last_content[:500]}"
                            )
                            deps.queue.enqueue_message_check(group_jid)
                        else:
                            logger.debug(
                                "Piped messages to active container",
                                chat_jid=group_jid,
                                count=len(all_pending),
                            )
                            last_msg = all_pending[-1]
                            await deps.send_reaction_to_channels(
                                group_jid, last_msg.id, last_msg.sender, "👀"
                            )

                            prev = deps.last_agent_timestamp.get(group_jid, "")
                            deps.last_agent_timestamp[group_jid] = all_pending[-1].timestamp
                            try:
                                await deps.save_state()
                            except Exception:
                                deps.last_agent_timestamp[group_jid] = prev
                                raise
                    else:
                        deps.queue.enqueue_message_check(group_jid)

        except Exception:
            logger.exception("Error in message loop")

        # Periodically reconcile channel history to recover events
        # dropped by Socket Mode or other transient delivery failures.
        now = _time.monotonic()
        if now - _last_catchup >= _CATCHUP_INTERVAL:
            _last_catchup = now
            try:
                await deps.catch_up_channels()
            except Exception:
                logger.exception("Error in channel catch-up")

        await asyncio.sleep(s.intervals.message_poll)
