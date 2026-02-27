"""Message processing pipeline — intercepts commands and processes messages for agents.

Handles command interception (reset, end session, redeploy, !commands),
reset handoffs, dirty repo checks, cursor management, and the core
group message processing flow.

Message routing and the polling loop live in :mod:`_message_routing`.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pynchy.chat.approval_handler import handle_approval_command, handle_pending_query
from pynchy.chat.commands import (
    is_approval_command,
    is_context_reset,
    is_end_session,
    is_pending_query,
    is_redeploy,
)
from pynchy.config import get_settings
from pynchy.db import get_messages_since, store_message_direct
from pynchy.event_bus import AgentActivityEvent, MessageEvent
from pynchy.git_ops.utils import is_repo_dirty
from pynchy.logger import logger
from pynchy.utils import generate_message_id, run_shell_command

if TYPE_CHECKING:
    from pynchy.container_runner import OnOutput
    from pynchy.group_queue import GroupQueue
    from pynchy.types import Channel, ContainerOutput, NewMessage, WorkspaceProfile


class MessageHandlerDeps(Protocol):
    """Dependencies for message processing."""

    @property
    def channels(self) -> list[Channel]: ...

    @property
    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    @property
    def last_agent_timestamp(self) -> dict[str, str]: ...

    # Transient (not persisted): furthest message timestamp dispatched to the
    # active container.  Distinct from last_agent_timestamp, which only advances
    # on successful completion.  Used by the routing loop as the get_messages_since
    # baseline so follow-up pipes don't re-include messages already being handled.
    @property
    def _dispatched_through(self) -> dict[str, str]: ...

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
        on_output: OnOutput | None = None,
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
    logger.info("intercept_trace", step="start", group=group.name, content=content[:50])

    # --- Commands that manage their own cursor (via _teardown_group) ---

    if is_context_reset(content):
        logger.info("intercept_trace", step="context_reset_start", group=group.name)
        await deps.handle_context_reset(chat_jid, group, message.timestamp)
        logger.info("Context reset", group=group.name)
        return True

    if is_end_session(content):
        logger.info("intercept_trace", step="end_session_start", group=group.name)
        await deps.handle_end_session(chat_jid, group, message.timestamp)
        logger.info("End session", group=group.name)
        return True

    # --- Redeploy: advance cursor BEFORE the call (process may die) ---

    if is_redeploy(content):
        deps.last_agent_timestamp[chat_jid] = message.timestamp
        await deps.save_state()
        await deps.trigger_manual_redeploy(chat_jid)
        return True

    # --- Commands with uniform post-handler cursor advancement ---

    if approval := is_approval_command(content):
        action, short_id = approval
        await handle_approval_command(deps, chat_jid, action, short_id, message.sender_name)
    elif is_pending_query(content):
        await handle_pending_query(deps, chat_jid)
    elif content.startswith("!") and content[1:]:
        await execute_direct_command(deps, chat_jid, group, message, content[1:])
    else:
        return False

    deps.last_agent_timestamp[chat_jid] = message.timestamp
    await deps.save_state()
    return True


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

    result = await run_shell_command(
        command,
        cwd=str(s.groups_dir / group.folder),
        timeout_seconds=30,
    )

    if result.start_error:
        await deps.broadcast_host_message(chat_jid, f"❌ Command failed: {result.start_error}")
        logger.error("Direct command error", group=group.name, error=result.start_error)
        return

    if result.timed_out:
        await deps.broadcast_host_message(chat_jid, "⏱️ Command timed out (30s limit)")
        logger.warning("Direct command timeout", group=group.name, command=command[:100])
        return

    if result.returncode == 0:
        output = result.stdout or "(no output)"
        status_emoji = "✅"
    else:
        output = result.stderr or result.stdout or "(no output)"
        status_emoji = "❌"

    ts = datetime.now(UTC).isoformat()
    output_text = f"{status_emoji} Command output (exit {result.returncode}):\n```\n{output}\n```"

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
        # Return None (not True) so the caller falls through to normal
        # message processing instead of silently skipping this cycle.
        return None

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
    except OSError as exc:
        logger.error("Error checking for dirty repo after reset", err=str(exc))
        dirty_check_file.unlink(missing_ok=True)
    return notices


async def _advance_cursor(
    deps: MessageHandlerDeps,
    chat_jid: str,
    new_cursor: str,
    previous_cursor: str,
) -> None:
    """Advance the agent timestamp cursor, rolling back on save failure."""
    deps.last_agent_timestamp[chat_jid] = new_cursor
    try:
        await deps.save_state()
    except Exception:
        deps.last_agent_timestamp[chat_jid] = previous_cursor
        raise


def _mark_dispatched(deps: MessageHandlerDeps, chat_jid: str, new_timestamp: str) -> None:
    """Record the furthest message timestamp dispatched to the active container.

    In-memory only — never persisted.  last_agent_timestamp is the true
    "processed" cursor and only advances on successful completion (or when
    partial output has already been sent, to avoid duplicate responses).

    The routing loop uses max(last_agent_timestamp, _dispatched_through) as the
    get_messages_since baseline so follow-up pipes don't re-include messages
    that are already being handled by the active container.
    """
    deps._dispatched_through[chat_jid] = new_timestamp


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
    if reset_result is False:
        # Handoff failed — return False so GroupQueue will retry.
        return False
    # reset_result is None (no file) or True (handoff ran) — fall through to
    # process any pending user messages in the same cycle.  The old code
    # returned True on a successful handoff, silently dropping the message
    # that triggered this run (e.g. the user's first message after a context
    # reset could sit unprocessed until the next incoming message).

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
    from pynchy.config_access import resolve_channel_config

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

    # Remember the current cursor so we can restore it if save_state fails at completion.
    previous_cursor = deps.last_agent_timestamp.get(chat_jid, "")

    # Mark dispatched (in-memory only).  last_agent_timestamp stays at previous_cursor
    # until the container finishes — on an unexpected kill the DB retains the pre-run
    # value so recover_pending_messages can re-find the boundary message on restart.
    _mark_dispatched(deps, chat_jid, missed_messages[-1].timestamp)

    process_start = time.monotonic()
    logger.info(
        "Processing messages",
        group=group.name,
        message_count=len(missed_messages),
        preview=missed_messages[-1].content[:200],
    )

    # Send emoji reaction on the last message to indicate agent is reading
    last_msg = missed_messages[-1]
    await deps.send_reaction_to_channels(chat_jid, last_msg.id, last_msg.sender, "🦞")

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
        if result.status == "error":
            had_error = True

    agent_result = await deps.run_agent(
        group, chat_jid, messages, on_output, reset_system_notices or None
    )

    process_ms = (time.monotonic() - process_start) * 1000
    await deps.set_typing_on_channels(chat_jid, False)
    deps.emit(AgentActivityEvent(chat_jid=chat_jid, active=False))

    logger.info(
        "Message processing complete",
        group=group.name,
        process_ms=round(process_ms),
        had_error=had_error,
        output_sent=output_sent_to_user,
    )

    # Pop the dispatched marker; include any follow-ups piped while this
    # container was running (tracked by the routing loop via _mark_dispatched).
    dispatched = deps._dispatched_through.pop(chat_jid, missed_messages[-1].timestamp)
    final_cursor = max(missed_messages[-1].timestamp, dispatched)

    if agent_result == "error" or had_error:
        if output_sent_to_user:
            # Partial output already sent — advance cursor to prevent a duplicate
            # response if the same messages are re-processed on the next trigger.
            await _advance_cursor(deps, chat_jid, final_cursor, previous_cursor)
            logger.warning(
                "Agent error after output was sent, advanced cursor to prevent retry duplicate",
                group=group.name,
            )
            return True
        await deps.broadcast_host_message(
            chat_jid, "⚠️ Agent error occurred. Will retry on next message."
        )
        logger.warning(
            "Agent error, cursor unchanged for retry",
            group=group.name,
        )
        return False

    # Success: advance the true processed cursor now that the container finished.
    await _advance_cursor(deps, chat_jid, final_cursor, previous_cursor)

    # Merge worktree commits into main and push for groups with repo_access
    from pynchy.git_ops._worktree_merge import background_merge_worktree

    background_merge_worktree(group)

    return True
