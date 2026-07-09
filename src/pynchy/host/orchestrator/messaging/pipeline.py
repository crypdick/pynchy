"""Message processing pipeline — intercepts commands and processes messages for agents.

Handles command interception (reset, end session, redeploy, !commands),
reset handoffs, dirty repo checks, cursor management, and the core
group message processing flow.

Message routing and the polling loop live in :mod:`_message_routing`.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import pynchy.types as types
from pynchy.config import get_settings
from pynchy.config.settings import Settings
from pynchy.event_bus import AgentActivityEvent, MessageEvent
from pynchy.host.container_manager import OnOutput
from pynchy.host.learning import capture as learning_capture
from pynchy.host.orchestrator.concurrency import GroupQueue
from pynchy.host.orchestrator.messaging import approval_handler, commands
from pynchy.host.orchestrator.messaging.cursor import advance_cursor
from pynchy.host.orchestrator.messaging.router import pop_last_result_ids
from pynchy.host.orchestrator.messaging.run_context import prepare_message_context
from pynchy.logger import logger
from pynchy.state import get_messages_since, store_message_direct
from pynchy.utils import generate_message_id, run_shell_command

type Group = types.WorkspaceProfile


@runtime_checkable
class MessageHandlerDeps(Protocol):
    """Dependencies for message processing."""

    @property
    def channels(self) -> list[types.Channel]: ...

    @property
    def workspaces(self) -> dict[str, Group]: ...

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

    async def handle_context_reset(self, chat_jid: str, group: Group, timestamp: str) -> None: ...

    async def handle_end_session(self, chat_jid: str, group: Group, timestamp: str) -> None: ...

    async def trigger_manual_redeploy(self, chat_jid: str) -> None: ...

    async def broadcast_to_channels(
        self, chat_jid: str, event: types.OutboundEvent, *, suppress_errors: bool = True
    ) -> None: ...

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...

    async def send_reaction_to_channels(
        self, chat_jid: str, message_id: str, sender: str, emoji: str
    ) -> None: ...

    async def send_reaction_to_outbound(
        self, chat_jid: str, per_channel_ids: dict[str, str], emoji: str
    ) -> None: ...

    def processing_ack_emoji(self, chat_jid: str) -> str | None: ...

    async def set_typing_on_channels(self, chat_jid: str, is_typing: bool) -> None: ...

    async def catch_up_channels(self) -> None: ...

    async def start_interactive_turn(self, chat_jid: str) -> None: ...

    def emit(self, event: Any) -> None: ...

    async def run_agent(
        self,
        group: Group,
        chat_jid: str,
        messages: list[dict[str, Any]],
        on_output: OnOutput | None = None,
        extra_system_notices: list[str] | None = None,
        *,
        input_source: str = "user",
    ) -> str: ...

    async def handle_streamed_output(
        self, chat_jid: str, group: Group, result: types.ContainerOutput
    ) -> bool: ...


async def intercept_special_command(
    deps: MessageHandlerDeps,
    chat_jid: str,
    group: types.WorkspaceProfile,
    message: types.NewMessage,
) -> bool:
    """Check for and handle special commands (reset, end session, redeploy, !cmd).

    Returns True if a command was intercepted and handled, False otherwise.
    """
    content = message.content.strip()
    logger.info("intercept_trace", step="start", group=group.name, content=content[:50])

    # --- Commands that manage their own cursor (via _teardown_group) ---

    if commands.is_context_reset(content):
        logger.info("intercept_trace", step="context_reset_start", group=group.name)
        await deps.handle_context_reset(chat_jid, group, message.timestamp)
        logger.info("Context reset", group=group.name)
        return True

    if commands.is_end_session(content):
        logger.info("intercept_trace", step="end_session_start", group=group.name)
        await deps.handle_end_session(chat_jid, group, message.timestamp)
        logger.info("End session", group=group.name)
        return True

    # --- Redeploy: advance cursor BEFORE the call (process may die) ---

    if commands.is_redeploy(content):
        await advance_cursor(deps, chat_jid, message.timestamp)
        await deps.trigger_manual_redeploy(chat_jid)
        return True

    # --- Commands with uniform post-handler cursor advancement ---

    if approval := commands.is_approval_command(content):
        action, short_id = approval
        await approval_handler.handle_approval_command(
            deps, chat_jid, action, short_id, message.sender_name
        )
    elif commands.is_pending_query(content):
        await approval_handler.handle_pending_query(deps, chat_jid)
    elif content.startswith("!") and content[1:]:
        await execute_direct_command(deps, chat_jid, group, message, content[1:])
    else:
        return False

    await advance_cursor(deps, chat_jid, message.timestamp)
    return True


async def execute_direct_command(
    deps: MessageHandlerDeps,
    chat_jid: str,
    group: types.WorkspaceProfile,
    message: types.NewMessage,
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
        output, status_emoji = result.stdout or "(no output)", "✅"
    else:
        output, status_emoji = result.stderr or result.stdout or "(no output)", "❌"

    ts = datetime.now(UTC).isoformat()
    output_text = f"{status_emoji} Command output (exit {result.returncode}):\n```\n{output}\n```"

    await store_message_direct(
        message_id=generate_message_id("cmd"),
        chat_jid=chat_jid,
        sender="command_output",
        sender_name="command",
        content=output_text,
        timestamp=ts,
        is_from_me=True,
        message_type="tool_result",
        metadata={"exit_code": result.returncode},
    )

    event = types.OutboundEvent(
        type=types.OutboundEventType.TOOL_RESULT,
        content=output_text,
        metadata={"verbose": True},
    )
    await deps.broadcast_to_channels(chat_jid, event)

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
    group: types.WorkspaceProfile,
    reset_file: Path,
) -> bool | None:
    """Consume a reset_prompt.json file and run the handoff agent.

    Returns True/False if the reset was handled (success/failure),
    or None if there was no reset to process.
    """
    if not await asyncio.to_thread(reset_file.exists):
        return None

    s = get_settings()
    try:
        reset_text = await asyncio.to_thread(reset_file.read_text, encoding="utf-8")
        reset_data = json.loads(reset_text)
        await asyncio.to_thread(reset_file.unlink)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "Failed to read reset prompt file",
            group=group.name,
            path=str(reset_file),
            err=str(exc),
        )
        await asyncio.to_thread(reset_file.unlink, missing_ok=True)
        # Return None (not True) so the caller falls through to normal
        # message processing instead of silently skipping this cycle.
        return None

    reset_message = reset_data.get("message", "")
    if not reset_message:
        return True

    logger.info("Processing reset handoff", group=group.name)

    async def handoff_on_output(result: types.ContainerOutput) -> None:
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


async def _should_skip_batch(
    deps: MessageHandlerDeps,
    chat_jid: str,
    group: types.WorkspaceProfile,
    missed_messages: list[types.NewMessage],
    is_admin_group: bool,
    s: Settings,
) -> bool:
    """True if this batch needs no agent activation (already handled or gated)."""
    if not missed_messages:
        return True

    # System notices alone shouldn't launch a container — they're context
    # for the next real session, not actionable messages.
    if all(m.sender == "system_notice" for m in missed_messages):
        return True

    # Intercept special commands before normal routing so commands like
    # context reset, end session, and redeploy are handled immediately.
    return await intercept_special_command(deps, chat_jid, group, missed_messages[-1])


async def _announce_processing_start(
    deps: MessageHandlerDeps,
    chat_jid: str,
    group: types.WorkspaceProfile,
    missed_messages: list[types.NewMessage],
) -> None:
    """Mark dispatched, log, and signal 'agent is working' to the user."""
    # Mark dispatched (in-memory only).  last_agent_timestamp stays at its pre-run value
    # until the container finishes — on an unexpected kill the DB retains the pre-run
    # value so recover_pending_messages can re-find the boundary message on restart.
    _mark_dispatched(deps, chat_jid, missed_messages[-1].timestamp)

    logger.info(
        "Processing messages",
        group=group.name,
        message_count=len(missed_messages),
        preview=missed_messages[-1].content[:200],
    )

    last_msg = missed_messages[-1]
    ack_emoji = deps.processing_ack_emoji(chat_jid)
    if ack_emoji:
        await deps.send_reaction_to_channels(chat_jid, last_msg.id, last_msg.sender, ack_emoji)

    # Set typing indicator on all channels that support it
    await deps.set_typing_on_channels(chat_jid, is_typing=True)

    deps.emit(AgentActivityEvent(chat_jid=chat_jid, active=True))


def _register_idle_zzz_callback(
    deps: MessageHandlerDeps,
    chat_jid: str,
    group: types.WorkspaceProfile,
    output_sent_to_user: bool,
) -> None:
    """Register a zzz reaction to fire when the container actually hibernates
    (idle timeout), not immediately after the query finishes.
    """
    outbound_ids = pop_last_result_ids(chat_jid)
    if not (outbound_ids and output_sent_to_user):
        return

    from pynchy.host.container_manager.session import get_session

    session = get_session(types.GroupFolder(group.folder))
    if session is None:
        return

    # Capture ids by value — the session may outlive these locals.
    ids = dict(outbound_ids)

    async def _send_zzz() -> None:
        await deps.send_reaction_to_outbound(chat_jid, ids, "zzz")

    session.set_idle_callback(_send_zzz)


async def _finalize_cursor_and_retry(
    deps: MessageHandlerDeps,
    chat_jid: str,
    group: types.WorkspaceProfile,
    missed_messages: list[types.NewMessage],
    agent_result: str,
    had_error: bool,
    output_sent_to_user: bool,
    learning_summary: learning_capture.LearningRunSummary,
    s: Settings,
) -> bool:
    """Advance the cursor (or signal retry) based on how the agent run went.

    Returns True once the batch is considered handled, False if GroupQueue
    should retry it.
    """
    # Pop the dispatched marker; include any follow-ups piped while this
    # container was running (tracked by the routing loop via _mark_dispatched).
    dispatched = deps._dispatched_through.pop(chat_jid, missed_messages[-1].timestamp)
    final_cursor = max(missed_messages[-1].timestamp, dispatched)

    failed = agent_result == "error" or had_error

    # The cursor advances in every case EXCEPT a clean failure with no
    # user-visible output — that's the only path we want re-tried verbatim.
    if failed and not output_sent_to_user:
        await deps.broadcast_host_message(
            chat_jid, "⚠️ Agent error occurred. Will retry on next message."
        )
        logger.warning("Agent error, cursor unchanged for retry", group=group.name)
        return False

    await advance_cursor(deps, chat_jid, final_cursor)

    if failed:
        # Partial output already sent — cursor advanced to prevent a duplicate
        # response if the same messages are re-processed on the next trigger.
        logger.warning(
            "Agent error after output was sent, advanced cursor to prevent retry duplicate",
            group=group.name,
        )
        return True

    await learning_capture.start_completed_turn_learning_review(
        s, chat_jid, group, missed_messages, final_cursor, learning_summary, get_messages_since
    )

    # Success: merge worktree commits into main and push for groups with repo_access
    from pynchy.host.git_ops._worktree_merge import background_merge_worktree

    background_merge_worktree(group)

    return True


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
    # process any pending user messages in the same cycle.  Falling through
    # ensures the message that triggered this run (e.g. the user's first
    # message after a context reset) is processed now rather than sitting
    # unprocessed until the next incoming message.

    is_admin_group = group.is_admin
    since_timestamp = deps.last_agent_timestamp.get(chat_jid, "")
    missed_messages = await get_messages_since(chat_jid, since_timestamp)

    if await _should_skip_batch(deps, chat_jid, group, missed_messages, is_admin_group, s):
        return True

    messages, reset_system_notices = prepare_message_context(
        s, group, missed_messages, is_admin_group
    )

    process_start = time.monotonic()
    await _announce_processing_start(deps, chat_jid, group, missed_messages)

    had_error = False
    output_sent_to_user = False
    learning_summary = learning_capture.LearningRunSummary()

    async def on_output(result: types.ContainerOutput) -> None:
        nonlocal had_error, output_sent_to_user

        learning_capture.observe_learning_output(learning_summary, result)
        sent = await deps.handle_streamed_output(chat_jid, group, result)
        if sent:
            output_sent_to_user = True
        if result.status == "error":
            had_error = True

    agent_result = await deps.run_agent(
        group, chat_jid, messages, on_output, reset_system_notices or None
    )

    process_ms = (time.monotonic() - process_start) * 1000
    await deps.set_typing_on_channels(chat_jid, is_typing=False)
    deps.emit(AgentActivityEvent(chat_jid=chat_jid, active=False))
    _register_idle_zzz_callback(deps, chat_jid, group, output_sent_to_user)

    logger.info(
        "Message processing complete",
        group=group.name,
        process_ms=round(process_ms),
        had_error=had_error,
        output_sent=output_sent_to_user,
    )

    return await _finalize_cursor_and_retry(
        deps,
        chat_jid,
        group,
        missed_messages,
        agent_result,
        had_error,
        output_sent_to_user,
        learning_summary,
        s,
    )
