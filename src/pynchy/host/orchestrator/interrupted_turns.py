"""Application use case for resuming one durable interrupted turn."""

from __future__ import annotations

from typing import cast

from pynchy.agent_protocol.api import (
    CheckpointControlState,
    InFlightWorkKind,
)
from pynchy.host.orchestrator.messaging import pipeline as messaging_pipeline
from pynchy.host.orchestrator.messaging.in_flight import resume_interrupted_message_turn
from pynchy.host.orchestrator.scheduled_binding import resolve_scheduled_group
from pynchy.host.orchestrator.task_scheduler import (
    SchedulerDependencies,
    resume_interrupted_scheduled_turn,
)
from pynchy.state.api import get_in_flight_turn, get_task_by_id, release_in_flight_turn_claim
from pynchy.turn_outcomes import TurnOutcome
from pynchy.workspace.api import RuntimeTarget


async def dispatch_interrupted_turn(turn_id: str, deps: object) -> TurnOutcome:
    """Resume a claimed turn through its scheduled or interactive use case."""
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
