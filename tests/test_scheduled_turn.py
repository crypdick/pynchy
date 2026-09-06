"""Scheduled agent-turn lifecycle tests."""

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.agent_protocol.api import CheckpointControlState, ContainerOutput
from pynchy.host.orchestrator.scheduled_turn import TaskAgentRequest, run_task_agent
from pynchy.host.orchestrator.scheduled_turn_deps import ScheduledTurnDeps, ScheduledTurnQueue
from pynchy.scheduling.api import ScheduledTask, SessionPolicy
from pynchy.state.api import (
    get_in_flight_turn_for_task,
    init_test_database,
    request_in_flight_turn_control,
)
from pynchy.turn_outcomes import TurnOutcome
from pynchy.workspace.api import WorkspaceProfile


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.loop = asyncio.get_running_loop()
        self.pending: list[tuple[asyncio.TimerHandle, Callable[[], None]]] = []

    def call_later(self, delay: float, callback: Callable[[], None]) -> asyncio.TimerHandle:
        handle = asyncio.TimerHandle(self.now + delay, callback, (), self.loop)
        self.pending.append((handle, callback))
        return handle

    def advance(self, seconds: float) -> None:
        self.now += seconds
        for handle, callback in tuple(self.pending):
            if handle.when() <= self.now:
                self.pending.remove((handle, callback))
                if not handle.cancelled():
                    callback()


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
    deps = MagicMock(spec=ScheduledTurnDeps)
    deps.queue = MagicMock(spec=ScheduledTurnQueue)
    deps.queue.boundary_interrupt_requested.return_value = False
    deps.queue.interrupt_after_tool_result = AsyncMock()
    deps.handle_streamed_output = AsyncMock(return_value=False)
    deps.run_agent = AsyncMock(return_value="success")
    return deps


@pytest.mark.asyncio
async def test_idle_timeout_tracks_latest_output() -> None:
    deps = _deps()
    clock = _Clock()

    async def run_agent(*args, **_kwargs) -> str:
        clock.advance(6)
        deps.queue.close_stdin.assert_not_called()
        await args[3](ContainerOutput(status="success", result="working"))
        clock.advance(6)
        deps.queue.close_stdin.assert_not_called()
        clock.advance(4)
        deps.queue.close_stdin.assert_called_once_with("project")
        return "success"

    deps.run_agent = run_agent
    request = TaskAgentRequest(task=_task(), deps=deps, group=_group(), idle_timeout=10.0)
    with patch.object(clock.loop, "call_later", clock.call_later):
        result = await run_task_agent(request)

    assert result.result == "working"
    assert result.error is None
    assert await get_in_flight_turn_for_task("task-1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["success", "error", "cancel"])
async def test_cancels_idle_timeout_when_agent_stops(ending: str) -> None:
    deps = _deps()
    deps.handle_streamed_output = AsyncMock(return_value=True)
    clock = _Clock()

    async def run_agent(*args, **_kwargs) -> str:
        await args[3](ContainerOutput(status="success", result="partial output"))
        if ending == "error":
            raise RuntimeError("agent failed")
        if ending == "cancel":
            raise asyncio.CancelledError
        return "success"

    deps.run_agent = run_agent
    request = TaskAgentRequest(task=_task(), deps=deps, group=_group(), idle_timeout=10.0)
    with patch.object(clock.loop, "call_later", clock.call_later):
        if ending == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await run_task_agent(request)
        else:
            result = await run_task_agent(request)
            assert result.result == "partial output"
            assert result.error == ("agent failed" if ending == "error" else None)
        clock.advance(100)
    deps.queue.close_stdin.assert_not_called()

    turn = await get_in_flight_turn_for_task("task-1")
    if ending == "success":
        assert turn is None
    else:
        assert turn is not None
        assert turn.output_sent is True
        assert (turn.claimed_at is None) is (ending == "error")


@pytest.mark.asyncio
async def test_run_task_agent_reports_a_mismatched_durable_binding() -> None:
    deps = _deps()
    request = TaskAgentRequest(
        task=_task(bound_chat_jid="discord:channel:other"),
        deps=deps,
        group=_group(),
        idle_timeout=1.0,
    )
    result = await run_task_agent(request)

    assert result.error == "Scheduled task runtime binding does not match its queue owner"
    assert await get_in_flight_turn_for_task("task-1") is None
    deps.run_agent.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_reset_request_survives_agent_exit(raises: bool) -> None:
    deps = _deps()

    async def run_agent(*_args, **_kwargs) -> str:
        await request_in_flight_turn_control(
            "discord:channel:project", CheckpointControlState.RESET_REQUESTED
        )
        if raises:
            raise RuntimeError("agent failed")
        return "error"

    deps.run_agent = run_agent
    request = TaskAgentRequest(task=_task(), deps=deps, group=_group(), idle_timeout=1.0)
    result = await run_task_agent(request)

    assert result.error is None
    assert result.terminal_outcome is TurnOutcome.RESET
    assert await get_in_flight_turn_for_task("task-1") is None
