"""Pause-aware message preparation and interactive agent execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pynchy.agent_protocol.api import (
    CheckpointControlState,
    ContainerOutput,
    InFlightTurn,
    InFlightWorkKind,
)
from pynchy.event_bus import AgentActivityEvent
from pynchy.host.orchestrator.messaging.deps import (
    MessageHandlerDeps,
)
from pynchy.host.orchestrator.messaging.host_controls import (
    mark_dispatched,
    should_skip_batch,
)
from pynchy.host.orchestrator.messaging.in_flight import (
    note_output_sent,
    requested_control_outcome,
    resume_interrupted_message_turn,
)
from pynchy.host.orchestrator.messaging.run_context import prepare_message_context
from pynchy.host.orchestrator.messaging.sender_policy import load_allowed_group_messages
from pynchy.host.orchestrator.messaging.turn_recovery import (
    handle_reset_handoff,
    resume_interrupted_message_if_present,
)
from pynchy.identifiers import RuntimeId
from pynchy.plugins.api import NewMessage
from pynchy.state.api import (
    clear_chat_pause,
    clear_in_flight_turn,
    get_oldest_resumable_turn_for_group,
    message_cursor,
    release_in_flight_turn_claim,
    resume_paused_in_flight_turn,
)
from pynchy.turn_outcomes import (
    TurnOutcome,
)
from pynchy.workspace.api import RuntimeTarget, WorkspaceProfile

ProcessPending = Callable[[str], Awaitable[TurnOutcome]]
_MESSAGE_WORK_KINDS = {
    InFlightWorkKind.INTERACTIVE,
    InFlightWorkKind.RESET_HANDOFF,
    InFlightWorkKind.SCHEDULED,
}


def _is_human_derived(message: NewMessage) -> bool:
    metadata = message.metadata or {}
    if message.message_type == "host" or message.sender == "system_notice" or message.is_from_me:
        return False
    if metadata.get("synthetic_user_input") is True:
        return False
    if metadata.get("authenticated_external_route") is True:
        return metadata.get("human_derived") is True
    return True


@dataclass(frozen=True)
class AgentBatch:
    """Prepared ordinary input that should start a fresh interactive checkpoint."""

    since_timestamp: str
    missed_messages: list[NewMessage]
    messages: list[dict[str, Any]]
    reset_system_notices: list[str]


async def prepare_agent_batch(
    deps: MessageHandlerDeps,
    chat_jid: str,
    group: WorkspaceProfile,
    data_dir: Path,
    process_pending: ProcessPending,
) -> AgentBatch | TurnOutcome:
    """Resume durable work or prepare pending messages for an interactive turn."""
    resumed = await resume_interrupted_message_if_present(
        deps,
        chat_jid,
        group,
        process_pending,
    )
    if resumed is not None:
        return resumed

    reset_file = data_dir / "ipc" / group.folder / "reset_prompt.json"
    if await handle_reset_handoff(deps, chat_jid, group, reset_file, data_dir) is False:
        return TurnOutcome.RETRY

    since_timestamp = deps.last_agent_timestamp.get(chat_jid, "")
    missed_messages = await load_allowed_group_messages(deps, chat_jid, group, since_timestamp)
    if any(_is_human_derived(message) for message in missed_messages):
        await clear_chat_pause(chat_jid)
    checkpoint = await get_oldest_resumable_turn_for_group(group.folder, _MESSAGE_WORK_KINDS)
    if await should_skip_batch(deps, chat_jid, group, missed_messages):
        checkpoint = await get_oldest_resumable_turn_for_group(
            group.folder,
            _MESSAGE_WORK_KINDS,
        )
        if checkpoint is not None and checkpoint.control_state in {
            CheckpointControlState.PAUSE_REQUESTED,
            CheckpointControlState.PAUSED,
        }:
            return TurnOutcome.PAUSED
        return TurnOutcome.COMPLETED

    messages, reset_system_notices = prepare_message_context(
        data_dir,
        group,
        missed_messages,
        is_admin_group=group.is_admin,
        repo_is_dirty=deps.repo_is_dirty,
    )
    batch = AgentBatch(
        since_timestamp=since_timestamp,
        missed_messages=missed_messages,
        messages=messages,
        reset_system_notices=reset_system_notices,
    )
    if checkpoint is None or checkpoint.control_state is CheckpointControlState.ACTIVE:
        return batch
    return await _resume_paused_checkpoint(
        deps, chat_jid, group, checkpoint, batch, process_pending
    )


async def _resume_paused_checkpoint(  # noqa: PLR0913 - reuse batch and checkpoint without a second request type.
    deps: MessageHandlerDeps,
    chat_jid: str,
    group: WorkspaceProfile,
    checkpoint: InFlightTurn,
    batch: AgentBatch,
    process_pending: ProcessPending,
) -> TurnOutcome:
    """Attach pending guidance and resume the frozen occurrence exactly once."""
    if checkpoint.control_state is CheckpointControlState.PAUSE_REQUESTED:
        # A reply can race the process shutdown. Its interactive workflow may
        # retry, but the paused occurrence itself never retries automatically.
        return TurnOutcome.RETRY
    if checkpoint.control_state is CheckpointControlState.RESET_REQUESTED:
        await clear_in_flight_turn(checkpoint.turn_id)
        return TurnOutcome.RESET
    is_scheduled = checkpoint.work_kind is InFlightWorkKind.SCHEDULED
    resumed = await resume_paused_in_flight_turn(
        checkpoint.turn_id,
        batch.messages,
        message_cursor(batch.missed_messages[-1]),
        claim=not is_scheduled,
    )
    if resumed is None:
        return TurnOutcome.COMPLETED

    mark_dispatched(deps, chat_jid, message_cursor(batch.missed_messages[-1]))
    if is_scheduled:
        await deps.start_interrupted_turn(resumed.turn_id, group.folder)
        return TurnOutcome.COMPLETED

    return await resume_interrupted_message_turn(
        deps,
        RuntimeTarget.from_binding(group.folder, chat_jid),
        group,
        resumed,
        process_pending,
    )


@dataclass(frozen=True)
class InteractiveAgentRun:
    """Agent result plus the stream evidence needed for turn finalization."""

    agent_result: str
    had_error: bool
    missing_terminal_result: bool
    output_sent_to_user: bool
    learning_summary: object
    control_outcome: TurnOutcome | None


async def _requested_control_after_agent_exit(
    deps: MessageHandlerDeps,
    chat_jid: str,
    turn_id: str,
    *,
    agent_succeeded: bool,
) -> TurnOutcome | None:
    outcome = await requested_control_outcome(
        turn_id,
        agent_succeeded=agent_succeeded,
    )
    if outcome is not None:
        await deps.set_typing_on_channels(chat_jid, is_typing=False)
        deps.emit(AgentActivityEvent(chat_jid=chat_jid, active=False))
    return outcome


async def run_interactive_agent(
    deps: MessageHandlerDeps,
    group: WorkspaceProfile,
    messages: list[dict[str, Any]],
    reset_system_notices: list[str],
    turn: InFlightTurn,
) -> InteractiveAgentRun:
    """Invoke one checkpointed interactive turn and settle control requests."""
    chat_jid = turn.chat_jid
    had_error = False
    missing_terminal_result = False
    terminal_result_observed = False
    output_sent_to_user = False
    learning_summary = deps.new_learning_run_summary()

    async def on_output(result: ContainerOutput) -> None:
        nonlocal had_error, missing_terminal_result, terminal_result_observed
        nonlocal output_sent_to_user

        deps.observe_learning_output(learning_summary, result)
        missing_terminal_result = missing_terminal_result or (
            (result.result_metadata or {}).get("subtype") == "missing_terminal_turn"
        )
        terminal_result_observed = terminal_result_observed or (
            result.type == "result" and result.status == "success" and bool(result.result)
        )
        sent = await deps.handle_streamed_output(
            chat_jid,
            group,
            result,
            turn_id=turn.turn_id,
        )
        if sent:
            await note_output_sent(turn.turn_id, already_recorded=output_sent_to_user)
            output_sent_to_user = True
        if result.status == "error":
            had_error = True
        if result.type == "tool_result":
            await deps.queue.interrupt_after_tool_result(RuntimeId(group.folder))

    control_outcome: TurnOutcome | None = None
    try:
        agent_result = await deps.run_agent(
            group,
            chat_jid,
            messages,
            on_output,
            reset_system_notices or None,
            input_source=turn.input_source,
            turn_id=turn.turn_id,
        )
    except asyncio.CancelledError:
        control_outcome = await _requested_control_after_agent_exit(
            deps,
            chat_jid,
            turn.turn_id,
            agent_succeeded=False,
        )
        if control_outcome is None:
            raise
        agent_result = "error"
    except BaseException:
        control_outcome = await _requested_control_after_agent_exit(
            deps,
            chat_jid,
            turn.turn_id,
            agent_succeeded=False,
        )
        if control_outcome is None:
            await release_in_flight_turn_claim(turn.turn_id)
            raise
        agent_result = "error"
    else:
        control_outcome = await _requested_control_after_agent_exit(
            deps,
            chat_jid,
            turn.turn_id,
            agent_succeeded=agent_result == "success" and not had_error,
        )
    return InteractiveAgentRun(
        agent_result=agent_result,
        had_error=had_error,
        missing_terminal_result=missing_terminal_result
        or (agent_result == "success" and not terminal_result_observed),
        output_sent_to_user=output_sent_to_user,
        learning_summary=learning_summary,
        control_outcome=control_outcome,
    )
