"""Focused public contracts for interrupted-turn Temporal activity."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import pynchy.host.orchestrator.temporal.interrupted as temporal_interrupted
import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler
from pynchy.agent_protocol.api import CheckpointControlState, InFlightTurn, InFlightWorkKind
from pynchy.state import begin_in_flight_turn, init_test_database
from pynchy.turn_outcomes import TurnOutcome
from tests.temporal_scheduler_support import NullSchedulerDeps


def _turn(turn_id: str, *, control_state: CheckpointControlState | None = None) -> InFlightTurn:
    return InFlightTurn(
        turn_id=turn_id,
        chat_jid="slack:C123",
        group_folder="admin",
        work_kind=InFlightWorkKind.INTERACTIVE,
        input_messages=[{"sender_name": "User", "content": "finish the job"}],
        input_start_cursor="old",
        input_end_cursor="new",
        started_at="2026-07-22T03:43:18+00:00",
        control_state=control_state or CheckpointControlState.ACTIVE,
    )


@pytest.mark.asyncio
async def test_reset_requested_interrupted_turn_settles_without_claiming() -> None:
    await init_test_database()
    await begin_in_flight_turn(
        _turn("turn-reset", control_state=CheckpointControlState.RESET_REQUESTED)
    )
    temporal_scheduler.bind_scheduler_deps(NullSchedulerDeps())

    result = await temporal_interrupted.run_interrupted_agent_turn("turn-reset")

    assert result == TurnOutcome.RESET.value


@pytest.mark.asyncio
async def test_missing_interrupted_turn_is_already_completed() -> None:
    await init_test_database()
    temporal_scheduler.bind_scheduler_deps(NullSchedulerDeps())

    assert (
        await temporal_interrupted.run_interrupted_agent_turn("turn-missing") == "already_completed"
    )


@pytest.mark.asyncio
async def test_already_claimed_interrupted_turn_is_not_dispatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await init_test_database()
    await begin_in_flight_turn(_turn("turn-claimed"))
    temporal_scheduler.bind_scheduler_deps(NullSchedulerDeps())
    claim = AsyncMock(return_value=False)
    dispatch = AsyncMock()
    monkeypatch.setattr(temporal_interrupted, "claim_in_flight_turn", claim)
    monkeypatch.setattr(temporal_interrupted, "_dispatch_interrupted_turn", dispatch)

    result = await temporal_interrupted.run_interrupted_agent_turn("turn-claimed")

    assert result == "already_claimed"
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_outcome_releases_interrupted_turn_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await init_test_database()
    await begin_in_flight_turn(_turn("turn-retry"))
    temporal_scheduler.bind_scheduler_deps(NullSchedulerDeps())
    release = AsyncMock()
    monkeypatch.setattr(temporal_interrupted, "release_in_flight_turn_claim", release)
    monkeypatch.setattr(
        temporal_interrupted,
        "_dispatch_interrupted_turn",
        AsyncMock(return_value=TurnOutcome.RETRY),
    )

    with pytest.raises(RuntimeError, match="requested retry"):
        await temporal_interrupted.run_interrupted_agent_turn("turn-retry")

    release.assert_awaited_once_with("turn-retry")
