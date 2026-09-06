"""Message processing pipeline — intercepts commands and processes messages for agents.

Handles command interception (reset, end session, redeploy, !commands),
reset handoffs, dirty repo checks, cursor management, and the core
group message processing flow.

Message routing and the polling loop live in :mod:`_message_routing`.
"""

from __future__ import annotations

import time

from pynchy.agent_protocol.api import InFlightTurn, InFlightWorkKind
from pynchy.conversation.api import ConversationClaimId, new_turn_id
from pynchy.event_bus import AgentActivityEvent
from pynchy.host.orchestrator.messaging import approval_handler, commands
from pynchy.host.orchestrator.messaging.cursor import (
    advance_cursor,
    complete_turn_with_cursor,
    monotonic_cursor,
)
from pynchy.host.orchestrator.messaging.deps import MessageHandlerDeps
from pynchy.host.orchestrator.messaging.direct_command import execute_direct_command
from pynchy.host.orchestrator.messaging.host_controls import (
    execute_deferred_host_controls as _execute_deferred_host_controls,
)
from pynchy.host.orchestrator.messaging.host_controls import (
    intercept_special_command,
    turn_boundary_lock,
)
from pynchy.host.orchestrator.messaging.host_controls import (
    mark_dispatched as _mark_dispatched,
)
from pynchy.host.orchestrator.messaging.in_flight import (
    MessageTurnStart,
    begin_message_turn,
)
from pynchy.host.orchestrator.messaging.router import pop_last_result_ids
from pynchy.host.orchestrator.messaging.turn_control import (
    AgentBatch,
    InteractiveAgentRun,
    prepare_agent_batch,
    run_interactive_agent,
)
from pynchy.identifiers import GroupFolder
from pynchy.logger import logger
from pynchy.plugins.api import (
    NewMessage,
)
from pynchy.state.api import (
    clear_in_flight_turn,
    message_cursor,
    release_in_flight_turn_claim,
)
from pynchy.turn_outcomes import (
    TurnOutcome,
)
from pynchy.workspace.api import RuntimeTarget, WorkspaceProfile

__all__ = [
    "MessageHandlerDeps",
    "approval_handler",
    "commands",
    "execute_direct_command",
    "intercept_special_command",
    "process_group_messages",
]


def _turn_id_for_batch(messages: list[NewMessage]) -> str:
    for message in reversed(messages):
        metadata = message.metadata or {}
        turn_id = metadata.get("turn_id")
        if isinstance(turn_id, str):
            return turn_id
    return new_turn_id()


def _input_source_for_batch(messages: list[NewMessage]) -> str:
    """Carry authenticated external provenance into sticky security taint."""
    public_providers = {
        str(metadata["external_provider"])
        for message in messages
        if (metadata := message.metadata or {})
        and metadata.get("authenticated_external_route") is True
        and metadata.get("public_source_input", True) is True
        and isinstance(metadata.get("external_provider"), str)
    }
    if public_providers:
        return f"external:{sorted(public_providers)[0]}"
    trusted_providers = {
        str(metadata["external_provider"])
        for message in messages
        if (metadata := message.metadata or {})
        and metadata.get("authenticated_external_route") is True
        and metadata.get("public_source_input") is False
        and isinstance(metadata.get("external_provider"), str)
    }
    return f"trusted:{sorted(trusted_providers)[0]}" if trusted_providers else "user"


def _conversation_claim_for_batch(
    messages: list[NewMessage],
) -> ConversationClaimId | None:
    claim_ids = {
        str(metadata["conversation_claim_id"])
        for message in messages
        if (metadata := message.metadata or {})
        and isinstance(metadata.get("conversation_claim_id"), str)
    }
    if len(claim_ids) > 1:
        raise RuntimeError("One agent turn cannot complete multiple conversation delivery claims")
    return ConversationClaimId(next(iter(claim_ids))) if claim_ids else None


async def _announce_processing_start(
    deps: MessageHandlerDeps,
    chat_jid: str,
    group: WorkspaceProfile,
    missed_messages: list[NewMessage],
) -> None:
    """Mark dispatched, log, and signal 'agent is working' to the user."""
    # Mark dispatched (in-memory only).  last_agent_timestamp stays at its pre-run value
    # until the container finishes — on an unexpected kill the DB retains the pre-run
    # value so recover_pending_messages can re-find the boundary message on restart.
    _mark_dispatched(deps, chat_jid, message_cursor(missed_messages[-1]))

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
    group: WorkspaceProfile,
    *,
    output_sent_to_user: bool,
) -> None:
    """Register a zzz reaction to fire when the container actually hibernates
    (idle timeout), not immediately after the query finishes.
    """
    outbound_ids = pop_last_result_ids(chat_jid)
    if not (outbound_ids and output_sent_to_user):
        return

    # Capture ids by value — the session may outlive these locals.
    ids = dict(outbound_ids)

    async def _send_zzz() -> None:
        await deps.send_reaction_to_outbound(chat_jid, ids, "zzz")

    deps.register_idle_callback(GroupFolder(group.folder), _send_zzz)


async def _finalize_cursor_and_retry(
    deps: MessageHandlerDeps,
    group: WorkspaceProfile,
    batch: AgentBatch,
    turn: InFlightTurn,
    agent_run: InteractiveAgentRun,
) -> TurnOutcome:
    """Advance the cursor (or signal retry) based on how the agent run went.

    A clean failure retains input for retry; visible partial output commits it
    to avoid duplicate replies.
    """
    failed = agent_run.agent_result == "error" or agent_run.had_error
    clean_failure = failed and not agent_run.output_sent_to_user
    async with turn_boundary_lock(turn.chat_jid):
        # Include follow-ups piped while this container was running. Active
        # host controls use this same in-memory boundary without persisting a
        # cursor ahead of an unfinished routed delivery claim.
        dispatched = deps.pop_dispatched(turn.chat_jid, message_cursor(batch.missed_messages[-1]))
        final_cursor = monotonic_cursor(message_cursor(batch.missed_messages[-1]), dispatched)

        if agent_run.missing_terminal_result:
            # Keep the routed claim and dispatch marker with the durable
            # checkpoint so recovery resumes this exact occurrence.
            await release_in_flight_turn_claim(turn.turn_id)
        elif clean_failure:
            await clear_in_flight_turn(turn.turn_id)
            # The durable inbound message carries this exact claim token. Retain
            # it so the next in-process attempt can finalize the same FIFO head;
            # startup recovery returns claims without a surviving turn to pending.
            # A control classifier that was waiting for this lock now observes
            # no active turn and cannot add another active-boundary marker.
            deps.pop_dispatched(turn.chat_jid, final_cursor)
        else:
            await complete_turn_with_cursor(
                deps,
                turn.chat_jid,
                final_cursor,
                turn.turn_id,
                conversation_claim_id=turn.conversation_claim_id,
            )
            late_dispatched = deps.pop_dispatched(
                turn.chat_jid,
                final_cursor,
            )
            final_cursor = monotonic_cursor(final_cursor, late_dispatched)
            previous = deps.last_agent_timestamp.get(turn.chat_jid, "")
            if monotonic_cursor(previous, final_cursor) != previous:
                await advance_cursor(deps, turn.chat_jid, final_cursor)

    if agent_run.missing_terminal_result:
        logger.warning(
            "Agent turn incomplete, cursor unchanged for retry",
            group=group.name,
        )
        return TurnOutcome.RETRY

    if clean_failure:
        await deps.broadcast_host_message(
            turn.chat_jid, "⚠️ Agent error occurred. Will retry on next message."
        )
        logger.warning("Agent error, cursor unchanged for retry", group=group.name)
        return TurnOutcome.RETRY

    await _execute_deferred_host_controls(
        deps,
        turn.chat_jid,
        group,
        batch.missed_messages,
    )

    if failed:
        # Partial output already sent — cursor advanced to prevent a duplicate
        # response if the same messages are re-processed on the next trigger.
        logger.warning(
            "Agent error after output was sent, advanced cursor to prevent retry duplicate",
            group=group.name,
        )
        return TurnOutcome.COMPLETED

    await deps.start_completed_turn_learning_review(
        turn.chat_jid,
        group,
        batch.missed_messages,
        final_cursor,
        agent_run.learning_summary,
    )

    return TurnOutcome.COMPLETED


async def _continue_after_host_turn(
    deps: MessageHandlerDeps,
    group: WorkspaceProfile,
    batch: AgentBatch,
    turn: InFlightTurn,
    agent_run: InteractiveAgentRun,
) -> TurnOutcome | None:
    """Commit a completed host boundary before Temporal starts its next activity."""
    if agent_run.agent_result == "interrupted":
        # The host process was stopped only after Codex reported a completed
        # tool. Keep later input pending and commit only the interrupted turn's
        # cursor so the next Temporal activity receives that follow-up.
        async with turn_boundary_lock(turn.chat_jid):
            deps.pop_dispatched(
                turn.chat_jid,
                message_cursor(batch.missed_messages[-1]),
            )
            await complete_turn_with_cursor(
                deps,
                turn.chat_jid,
                message_cursor(batch.missed_messages[-1]),
                turn.turn_id,
                conversation_claim_id=turn.conversation_claim_id,
            )
            deps.pop_dispatched(
                turn.chat_jid,
                message_cursor(batch.missed_messages[-1]),
            )
        await _execute_deferred_host_controls(
            deps,
            turn.chat_jid,
            group,
            batch.missed_messages,
        )
        return TurnOutcome.CONTINUE_AFTER_SAFE_INTERRUPT

    return None


async def _begin_interactive_message_turn(
    chat_jid: str,
    group: WorkspaceProfile,
    batch: AgentBatch,
) -> InFlightTurn:
    missed_messages = batch.missed_messages
    turn_id = _turn_id_for_batch(missed_messages)
    return await begin_message_turn(
        MessageTurnStart(
            turn_id=turn_id,
            chat_jid=chat_jid,
            group=group,
            work_kind=InFlightWorkKind.INTERACTIVE,
            input_messages=batch.messages,
            input_start_cursor=batch.since_timestamp,
            input_end_cursor=message_cursor(missed_messages[-1]),
            conversation_claim_id=_conversation_claim_for_batch(missed_messages),
            input_source=_input_source_for_batch(missed_messages),
        )
    )


async def _announce_processing_complete(
    deps: MessageHandlerDeps,
    chat_jid: str,
    group: WorkspaceProfile,
    agent_run: InteractiveAgentRun,
    process_start: float,
) -> None:
    await deps.set_typing_on_channels(chat_jid, is_typing=False)
    deps.emit(AgentActivityEvent(chat_jid=chat_jid, active=False))
    _register_idle_zzz_callback(
        deps,
        chat_jid,
        group,
        output_sent_to_user=agent_run.output_sent_to_user,
    )
    logger.info(
        "Message processing complete",
        group=group.name,
        process_ms=round((time.monotonic() - process_start) * 1000),
        had_error=agent_run.had_error,
        output_sent=agent_run.output_sent_to_user,
    )


async def process_group_messages(
    deps: MessageHandlerDeps,
    chat_jid: str,
) -> TurnOutcome:
    """Process all pending messages for a group. Called by GroupQueue."""
    group = deps.workspaces.get(chat_jid)
    if not group:
        return TurnOutcome.COMPLETED

    prepared = await prepare_agent_batch(
        deps,
        chat_jid,
        group,
        deps.message_data_dir,
        lambda jid: process_group_messages(deps, jid),
    )
    if not isinstance(prepared, AgentBatch):
        return prepared
    turn = await _begin_interactive_message_turn(chat_jid, group, prepared)
    turn_id = turn.turn_id
    try:
        process_start = time.monotonic()
        await _announce_processing_start(deps, chat_jid, group, prepared.missed_messages)
    except BaseException:
        await clear_in_flight_turn(turn_id)
        # Keep a routed claim attached to its durable input for the same retry
        # semantics as a clean agent failure.
        raise

    agent_run = await run_interactive_agent(
        deps,
        group,
        prepared.messages,
        prepared.reset_system_notices,
        turn,
    )
    if agent_run.control_outcome is not None:
        return agent_run.control_outcome

    await _announce_processing_complete(
        deps,
        chat_jid,
        group,
        agent_run,
        process_start,
    )

    try:
        continuation = await _continue_after_host_turn(deps, group, prepared, turn, agent_run)
        if continuation is not None:
            return continuation
        return await _finalize_cursor_and_retry(deps, group, prepared, turn, agent_run)
    except BaseException:
        await release_in_flight_turn_claim(turn_id)
        raise


async def run_queued_message_turn(
    deps: MessageHandlerDeps,
    chat_jid: str,
) -> TurnOutcome:
    """Enter the shared runtime queue before processing interactive messages."""
    group = deps.workspaces.get(chat_jid)
    if group is None:
        return TurnOutcome.COMPLETED
    return await deps.queue.run_message_turn(RuntimeTarget.from_workspace(group))
