"""Scheduled agent-turn lifecycle tests."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.host.orchestrator.scheduled_turn import (
    TaskAgentRequest,
    TaskAgentResult,
    run_task_agent,
)
from pynchy.scheduling.api import ScheduledTask, SessionPolicy
from pynchy.turn_outcomes import TurnOutcome
from pynchy.workspace.api import WorkspaceProfile


def _task(*, bound_chat_jid: str = "discord:channel:project") -> ScheduledTask:
    return ScheduledTask(
        id="task-1",
        group_folder="project",
        chat_jid="discord:channel:project",
        prompt="check the task",
        schedule_type="once",
        schedule_value="2026-07-29T00:00:00+00:00",
        session_policy=SessionPolicy.CONTINUE,
        bound_chat_jid=bound_chat_jid,
        bound_group_folder="project",
    )


def _group() -> WorkspaceProfile:
    return WorkspaceProfile(
        jid="discord:channel:project",
        name="Project",
        folder="project",
        trigger="@Pynchy",
    )


def _deps() -> MagicMock:
    deps = MagicMock()
    deps.queue.boundary_interrupt_requested.return_value = False
    deps.queue.interrupt_after_tool_result = AsyncMock()
    deps.handle_streamed_output = AsyncMock(return_value=False)
    deps.run_agent = AsyncMock(return_value="success")
    return deps


def test_task_agent_result_resolves_runtime_turn_outcome() -> None:
    result = TaskAgentResult("turn-1", None, None, terminal_outcome=TurnOutcome.RETRY)

    assert result.terminal_outcome is TurnOutcome.RETRY


@pytest.mark.asyncio
async def test_run_task_agent_closes_stdin_after_an_idle_timeout() -> None:
    deps = _deps()

    async def run_agent(*_args, **_kwargs) -> str:
        await asyncio.sleep(0.01)
        return "success"

    deps.run_agent = run_agent
    request = TaskAgentRequest(
        task=_task(),
        deps=deps,
        group=_group(),
        idle_enabled=True,
        idle_timeout=0.001,
    )
    with (
        patch(
            "pynchy.host.orchestrator.scheduled_turn.begin_message_turn",
            new_callable=AsyncMock,
        ),
        patch(
            "pynchy.host.orchestrator.scheduled_turn.requested_control_outcome",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "pynchy.host.orchestrator.scheduled_turn.clear_in_flight_turn",
            new_callable=AsyncMock,
        ),
    ):
        await run_task_agent(request)

    deps.queue.close_stdin.assert_called_once()


@pytest.mark.asyncio
async def test_run_task_agent_reports_a_mismatched_durable_binding() -> None:
    request = TaskAgentRequest(
        task=_task(bound_chat_jid="discord:channel:other"),
        deps=_deps(),
        group=_group(),
        idle_enabled=False,
        idle_timeout=1,
    )
    with patch(
        "pynchy.host.orchestrator.scheduled_turn.requested_control_outcome",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await run_task_agent(request)

    assert result.error == "Scheduled task runtime binding does not match its queue owner"


@pytest.mark.asyncio
async def test_run_task_agent_returns_a_controlled_terminal_outcome() -> None:
    request = TaskAgentRequest(
        task=_task(),
        deps=_deps(),
        group=_group(),
        idle_enabled=False,
        idle_timeout=1,
    )
    with (
        patch(
            "pynchy.host.orchestrator.scheduled_turn.begin_message_turn",
            new_callable=AsyncMock,
        ),
        patch(
            "pynchy.host.orchestrator.scheduled_turn.requested_control_outcome",
            new_callable=AsyncMock,
            return_value=TurnOutcome.COMPLETED,
        ),
    ):
        result = await run_task_agent(request)

    assert result.error is None
    assert result.terminal_outcome is TurnOutcome.COMPLETED
