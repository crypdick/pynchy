"""Temporal activity that dispatches a durable interrupted-turn checkpoint."""

from __future__ import annotations

from typing import cast

from temporalio import activity

from pynchy.host.orchestrator.messaging import pipeline as messaging_pipeline
from pynchy.host.orchestrator.messaging.in_flight import resume_interrupted_message_turn
from pynchy.host.orchestrator.messaging.outcomes import (
    CONTINUE_AFTER_SAFE_INTERRUPT,
    ProcessGroupResult,
)
from pynchy.host.orchestrator.task_scheduler import (
    SchedulerDependencies,
    resume_interrupted_scheduled_turn,
)
from pynchy.host.orchestrator.temporal.heartbeats import activity_heartbeats
from pynchy.host.orchestrator.temporal.runtime_state import (
    _record_activity_result,
    _require_scheduler_deps,
)
from pynchy.host.orchestrator.temporal.workflows import (
    CONTINUE_AFTER_SAFE_INTERRUPT as CONTINUE_AFTER_SAFE_INTERRUPT_RESULT,
)
from pynchy.state import (
    claim_in_flight_turn,
    get_in_flight_turn,
    get_task_by_id,
    release_in_flight_turn_claim,
)
from pynchy.types import InFlightWorkKind


async def _dispatch_interrupted_turn(turn_id: str, deps: object) -> ProcessGroupResult:
    turn = await get_in_flight_turn(turn_id)
    if turn is None:
        return True
    if turn.work_kind is InFlightWorkKind.SCHEDULED:
        if not turn.task_id:
            await release_in_flight_turn_claim(turn_id)
            raise RuntimeError("Interrupted scheduled turn has no task_id")
        task = await get_task_by_id(turn.task_id)
        if task is None:
            await release_in_flight_turn_claim(turn_id)
            raise RuntimeError(f"Interrupted scheduled task no longer exists: {turn.task_id}")
        return await resume_interrupted_scheduled_turn(
            task,
            cast("SchedulerDependencies", deps),
            turn,
        )

    message_deps = cast("messaging_pipeline.MessageHandlerDeps", deps)
    group = message_deps.workspaces.get(turn.chat_jid)
    if group is None:
        await release_in_flight_turn_claim(turn_id)
        raise RuntimeError(f"Interrupted turn workspace no longer exists: {turn.chat_jid}")
    return await resume_interrupted_message_turn(
        message_deps,
        group,
        turn,
        lambda jid: messaging_pipeline.process_group_messages(message_deps, jid),
    )


@activity.defn(name="run_interrupted_agent_turn")
async def run_interrupted_agent_turn(turn_id: str) -> str:
    """Claim and resume one interrupted turn independently of its source workflow."""
    turn = await get_in_flight_turn(turn_id)
    if turn is None:
        _record_activity_result(turn_id, "already_completed")
        return "already_completed"
    if not await claim_in_flight_turn(turn_id):
        _record_activity_result(turn_id, "already_claimed")
        return "already_claimed"

    try:
        async with activity_heartbeats(turn_id):
            handled = await _dispatch_interrupted_turn(turn_id, _require_scheduler_deps())
    except Exception as exc:  # noqa: BLE001, RUF100 - activity boundary records and retries failures.
        _record_activity_result(turn_id, "error", str(exc))
        raise

    if handled is CONTINUE_AFTER_SAFE_INTERRUPT:
        _record_activity_result(turn_id, CONTINUE_AFTER_SAFE_INTERRUPT_RESULT)
        return CONTINUE_AFTER_SAFE_INTERRUPT_RESULT
    if not handled:
        err = "Interrupted agent turn requested retry"
        _record_activity_result(turn_id, "retry_requested", err)
        raise RuntimeError(err)
    _record_activity_result(turn_id, "completed")
    return "completed"
