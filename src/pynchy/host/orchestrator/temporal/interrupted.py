"""Temporal activity that dispatches a durable interrupted-turn checkpoint."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from temporalio import activity

from pynchy.host.orchestrator.api import dispatch_interrupted_turn
from pynchy.host.orchestrator.temporal.heartbeats import activity_heartbeats
from pynchy.host.orchestrator.temporal.runtime_state import (
    _record_activity_result,
    _require_scheduler_deps,
    settle_turn_activity,
)
from pynchy.state.api import (
    claim_in_flight_turn,
    get_in_flight_turn,
    release_in_flight_turn_claim,
)
from pynchy.turn_outcomes import TurnOutcome
from pynchy.types import CheckpointControlState, InFlightTurn

if TYPE_CHECKING:
    from pynchy.host.orchestrator.scheduler_deps import SchedulerDependencies

_dispatch_interrupted_turn = dispatch_interrupted_turn


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
