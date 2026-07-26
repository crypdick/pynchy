"""Temporal activity that dispatches a durable interrupted-turn checkpoint."""

from __future__ import annotations

import asyncio
from typing import cast

from temporalio import activity

from pynchy.host.orchestrator.execution_outcomes import TurnOutcome
from pynchy.host.orchestrator.messaging import pipeline as messaging_pipeline
from pynchy.host.orchestrator.messaging.in_flight import resume_interrupted_message_turn
from pynchy.host.orchestrator.runtime_target import RuntimeTarget
from pynchy.host.orchestrator.scheduled_binding import resolve_scheduled_group
from pynchy.host.orchestrator.task_scheduler import (
    SchedulerDependencies,
    resume_interrupted_scheduled_turn,
)
from pynchy.host.orchestrator.temporal.heartbeats import activity_heartbeats
from pynchy.host.orchestrator.temporal.runtime_state import (
    _record_activity_result,
    _require_scheduler_deps,
    settle_turn_activity,
)
from pynchy.state import (
    claim_in_flight_turn,
    get_in_flight_turn,
    get_task_by_id,
    release_in_flight_turn_claim,
)
from pynchy.types import CheckpointControlState, InFlightTurn, InFlightWorkKind


def _terminal_control_outcome(
    turn: InFlightTurn,
) -> TurnOutcome | None:
    control_state = turn.control_state
    if control_state in {
        CheckpointControlState.PAUSE_REQUESTED,
        CheckpointControlState.PAUSED,
    }:
        return TurnOutcome.PAUSED
    if control_state is CheckpointControlState.RESET_REQUESTED:
        return TurnOutcome.RESET
    return None


async def _finish_interrupted_activity(
    turn_id: str,
    outcome: TurnOutcome,
) -> str:
    if outcome is TurnOutcome.RETRY:
        await release_in_flight_turn_claim(turn_id)
    return settle_turn_activity(
        turn_id,
        outcome,
        retry_error="Interrupted agent turn requested retry",
    )


async def _dispatch_interrupted_turn(turn_id: str, deps: object) -> TurnOutcome:
    turn = await get_in_flight_turn(turn_id)
    if turn is None:
        return TurnOutcome.COMPLETED
    if turn.control_state in {
        CheckpointControlState.PAUSE_REQUESTED,
        CheckpointControlState.PAUSED,
    }:
        return TurnOutcome.PAUSED
    if turn.control_state is CheckpointControlState.RESET_REQUESTED:
        return TurnOutcome.RESET
    if turn.work_kind is InFlightWorkKind.SCHEDULED:
        if not turn.task_id:
            await release_in_flight_turn_claim(turn_id)
            raise RuntimeError("Interrupted scheduled turn has no task_id")
        task = await get_task_by_id(turn.task_id)
        if task is None:
            await release_in_flight_turn_claim(turn_id)
            raise RuntimeError(f"Interrupted scheduled task no longer exists: {turn.task_id}")
        scheduler_deps = cast("SchedulerDependencies", deps)
        group = resolve_scheduled_group(scheduler_deps.workspaces, turn.group_folder)
        if group is None:
            await release_in_flight_turn_claim(turn_id)
            raise RuntimeError(
                f"Interrupted scheduled runtime no longer exists: {turn.group_folder}"
            )
        resolved_group = group

        async def resume_scheduled() -> TurnOutcome:
            return await resume_interrupted_scheduled_turn(
                task,
                scheduler_deps,
                turn,
                resolved_group,
            )

        return await scheduler_deps.queue.run_serialized_task(
            RuntimeTarget.from_workspace(resolved_group),
            f"recovery:{turn_id}",
            resume_scheduled,
        )

    message_deps = cast("messaging_pipeline.MessageHandlerDeps", deps)
    group = next(
        (
            workspace
            for workspace in message_deps.workspaces.values()
            if workspace.folder == turn.group_folder
        ),
        None,
    )
    if group is None:
        await release_in_flight_turn_claim(turn_id)
        raise RuntimeError(f"Interrupted turn runtime no longer exists: {turn.group_folder}")

    target = RuntimeTarget.from_workspace(group)

    async def resume_interactive() -> TurnOutcome:
        return await resume_interrupted_message_turn(
            message_deps,
            target,
            group,
            turn,
            lambda _jid: messaging_pipeline.process_group_messages(message_deps, group.jid),
        )

    return await message_deps.queue.run_serialized_task(
        target,
        f"recovery:{turn_id}",
        resume_interactive,
    )


@activity.defn(name="run_interrupted_agent_turn")
async def run_interrupted_agent_turn(turn_id: str) -> str:
    """Claim and resume one interrupted turn independently of its source workflow."""
    deps = cast("SchedulerDependencies", _require_scheduler_deps())
    async with activity_heartbeats(turn_id):
        await deps.startup_readiness.wait()

    turn = await get_in_flight_turn(turn_id)
    if turn is None:
        _record_activity_result(turn_id, "already_completed")
        return "already_completed"
    if terminal_outcome := _terminal_control_outcome(turn):
        return settle_turn_activity(
            turn_id,
            terminal_outcome,
            retry_error="Interrupted agent turn requested retry",
        )
    if not await claim_in_flight_turn(turn_id):
        _record_activity_result(turn_id, "already_claimed")
        return "already_claimed"

    try:
        async with activity_heartbeats(turn_id):
            handled = await _dispatch_interrupted_turn(turn_id, deps)

    except asyncio.CancelledError:
        # Temporal closes a cancelled workflow before this activity can report a
        # terminal event. Retain the checkpoint, but relinquish this process's
        # ownership so startup recovery or the next interactive trigger can
        # safely claim it again.
        await release_in_flight_turn_claim(turn_id)
        _record_activity_result(turn_id, "cancelled")
        raise
    except BaseException as exc:  # noqa: BLE001, RUF100 - claim ownership must end with the activity.
        await release_in_flight_turn_claim(turn_id)
        _record_activity_result(turn_id, "error", str(exc))
        raise

    return await _finish_interrupted_activity(turn_id, handled)
