"""Public Temporal workflow-loop contracts."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import pynchy.host.orchestrator.temporal.workflows as temporal_workflows
from pynchy.turn_outcomes import TurnOutcome


@pytest.mark.asyncio
async def test_interactive_message_workflow_retries_after_safe_interrupt(monkeypatch):
    execute_activity = AsyncMock(
        side_effect=[
            TurnOutcome.CONTINUE_AFTER_SAFE_INTERRUPT.value,
            TurnOutcome.COMPLETED.value,
        ]
    )
    monkeypatch.setattr(temporal_workflows.workflow, "execute_activity", execute_activity)

    result = await temporal_workflows.InteractiveMessageWorkflow().run("slack:C123", 3, 5.0)

    assert result == TurnOutcome.COMPLETED.value
    assert execute_activity.await_count == 2
    assert execute_activity.await_args_list[0].args == (
        "run_interactive_message_turn",
        "slack:C123",
    )


@pytest.mark.asyncio
async def test_interrupted_turn_workflow_runs_runtime_continuation_after_safe_interrupt(
    monkeypatch,
):
    execute_activity = AsyncMock(
        side_effect=[
            TurnOutcome.CONTINUE_AFTER_SAFE_INTERRUPT.value,
            TurnOutcome.COMPLETED.value,
        ]
    )
    monkeypatch.setattr(temporal_workflows.workflow, "execute_activity", execute_activity)

    result = await temporal_workflows.InterruptedTurnWorkflow().run("turn-1", "admin", 3, 5.0)

    assert result == TurnOutcome.COMPLETED.value
    assert execute_activity.await_count == 2
    assert execute_activity.await_args_list[1].args == ("run_interactive_runtime_turn", "admin")
