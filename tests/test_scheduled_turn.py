"""Scheduled agent-turn lifecycle tests."""

from pynchy.host.orchestrator.execution_outcomes import TurnOutcome
from pynchy.host.orchestrator.scheduled_turn import TaskAgentResult


def test_task_agent_result_resolves_runtime_turn_outcome() -> None:
    result = TaskAgentResult("turn-1", None, None, terminal_outcome=TurnOutcome.RETRY)

    assert result.terminal_outcome is TurnOutcome.RETRY
