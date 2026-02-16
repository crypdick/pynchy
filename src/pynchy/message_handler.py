"""Message processing pipeline — intercepts commands and routes messages to agents.

Extracted from app.py to keep the orchestrator focused on wiring.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from pynchy.commands import is_context_reset, is_end_session, is_redeploy
from pynchy.config import get_settings
from pynchy.db import get_messages_since, get_new_messages, store_message_direct
from pynchy.event_bus import AgentActivityEvent, MessageEvent
from pynchy.git_utils import is_repo_dirty
from pynchy.logger import logger
from pynchy.utils import IdleTimer, create_background_task

if TYPE_CHECKING:
    from pynchy.group_queue import GroupQueue
    from pynchy.types import ContainerOutput, NewMessage, RegisteredGroup


class MessageHandlerDeps(Protocol):
    """Dependencies for message processing."""

    @property
    def registered_groups(self) -> dict[str, RegisteredGroup]: ...

    @property
    def last_agent_timestamp(self) -> dict[str, str]: ...

    # The "seen" cursor for the polling loop (distinct from per-group agent cursors)
    last_timestamp: str

    @property
    def queue(self) -> GroupQueue: ...

    async def save_state(self) -> None: ...

    async def handle_context_reset(
        self, chat_jid: str, group: RegisteredGroup, timestamp: str
    ) -> None: ...

    async def handle_end_session(
        self, chat_jid: str, group: RegisteredGroup, timestamp: str
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

    def emit(self, event: Any) -> None: ...

    async def run_agent(
        self,
        group: RegisteredGroup,
        chat_jid: str,
        messages: list[dict],
        on_output: Any | None = None,
        extra_system_notices: list[str] | None = None,
    ) -> str: ...

    async def handle_streamed_output(
        self, chat_jid: str, group: RegisteredGroup, result: ContainerOutput
    ) -> bool: ...


async def intercept_special_command(
    deps: MessageHandlerDeps,
    chat_jid: str,
    group: RegisteredGroup,
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
    group: RegisteredGroup,
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
            id=f"cmd-{int(datetime.now(UTC).timestamp() * 1000)}",
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


async def process_group_messages(
    deps: MessageHandlerDeps,
    chat_jid: str,
) -> bool:
    """Process all pending messages for a group. Called by GroupQueue."""
    s = get_settings()
    group = deps.registered_groups.get(chat_jid)
    if not group:
        return True

    # Check for agent-initiated context reset prompt
    reset_file = s.data_dir / "ipc" / group.folder / "reset_prompt.json"
    if reset_file.exists():
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
        if reset_message:
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

            result = await deps.run_agent(group, chat_jid, reset_messages, handoff_on_output)

            if reset_data.get("needsDirtyRepoCheck"):
                dirty_check_file = s.data_dir / "ipc" / group.folder / "needs_dirty_check.json"
                dirty_check_file.write_text(
                    json.dumps({"timestamp": datetime.now(UTC).isoformat()})
                )

            return result != "error"
        return True

    is_god_group = group.is_god
    since_timestamp = deps.last_agent_timestamp.get(chat_jid, "")
    missed_messages = await get_messages_since(chat_jid, since_timestamp)

    if not missed_messages:
        return True

    # For non-god groups, check if trigger is required and present
    if not is_god_group and group.requires_trigger is not False:
        has_trigger = any(s.trigger_pattern.search(m.content.strip()) for m in missed_messages)
        if not has_trigger:
            return True

    # Intercept special commands
    if await intercept_special_command(deps, chat_jid, group, missed_messages[-1]):
        return True

    from pynchy.router import format_messages_for_sdk

    messages = format_messages_for_sdk(missed_messages)

    # Check if we need to add dirty repo warning after context reset
    reset_system_notices: list[str] = []
    dirty_check_file = s.data_dir / "ipc" / group.folder / "needs_dirty_check.json"
    if dirty_check_file.exists() and is_god_group:
        try:
            dirty_check_file.unlink()
            if is_repo_dirty():
                reset_system_notices.append(
                    "WARNING: Uncommitted changes detected in the repository. "
                    "Please review and commit these changes so that you may work "
                    "with a clean slate. "
                    "Run `git status` and `git diff` to see what has changed."
                )
                logger.info(
                    "Added dirty repo warning after reset",
                    group=group.name,
                )
        except Exception as exc:
            logger.error(
                "Error checking for dirty repo after reset",
                err=str(exc),
            )
            dirty_check_file.unlink(missing_ok=True)

    # Advance cursor; save old cursor for rollback on error.
    # Persist to DB first — if save_state fails, keep in-memory cursor
    # at the old position so we don't silently skip messages.
    previous_cursor = deps.last_agent_timestamp.get(chat_jid, "")
    new_cursor = missed_messages[-1].timestamp
    deps.last_agent_timestamp[chat_jid] = new_cursor
    try:
        await deps.save_state()
    except Exception:
        deps.last_agent_timestamp[chat_jid] = previous_cursor
        raise

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

    # Merge worktree commits into main and push for all project_access groups
    from pynchy.workspace_config import has_project_access
    from pynchy.worktree import merge_and_push_worktree

    if has_project_access(group):
        create_background_task(
            asyncio.to_thread(merge_and_push_worktree, group.folder),
            name=f"worktree-merge-{group.folder}",
        )

    return True


async def start_message_loop(
    deps: MessageHandlerDeps,
    shutting_down: Callable[[], bool],
) -> None:
    """Main polling loop — checks for new messages every message_poll interval."""
    s = get_settings()

    logger.info(f"Pynchy running (trigger: @{s.agent.name})")

    while not shutting_down():
        try:
            jids = list(deps.registered_groups.keys())
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
                    group = deps.registered_groups.get(group_jid)
                    if not group:
                        continue

                    is_god_group = group.is_god
                    needs_trigger = not is_god_group and group.requires_trigger is not False

                    if needs_trigger:
                        has_trigger = any(
                            s.trigger_pattern.search(m.content.strip()) for m in group_messages
                        )
                        if not has_trigger:
                            continue

                    all_pending = await get_messages_since(
                        group_jid,
                        deps.last_agent_timestamp.get(group_jid, ""),
                    )
                    if not all_pending:
                        continue

                    if await intercept_special_command(deps, group_jid, group, all_pending[-1]):
                        continue

                    formatted = "\n".join(
                        f"{msg.sender_name}: {msg.content}" for msg in all_pending
                    )

                    if deps.queue.is_active_task(group_jid):
                        last_content = all_pending[-1].content.strip()
                        if last_content.lower().startswith("btw "):
                            # Non-interrupting — forward to the running
                            # container via IPC as additional context.
                            deps.queue.send_message(group_jid, formatted)
                            deps.last_agent_timestamp[group_jid] = all_pending[-1].timestamp
                            await deps.save_state()
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
                            deps.last_agent_timestamp[group_jid] = all_pending[-1].timestamp
                            await deps.save_state()
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

        await asyncio.sleep(s.intervals.message_poll)
