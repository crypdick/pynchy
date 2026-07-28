"""Public recovery behavior for durable interrupted agent turns."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from pynchy.agent_protocol.api import CheckpointControlState, InFlightTurn, InFlightWorkKind
from pynchy.host.orchestrator.api import dispatch_interrupted_turn
from pynchy.scheduling.api import ScheduledTask, SessionPolicy
from pynchy.turn_outcomes import TurnOutcome
from pynchy.workspace.api import RuntimeTarget, WorkspaceProfile

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@dataclass
class _Queue:
    requests: list[tuple[RuntimeTarget, str]] = field(default_factory=list)

    async def run_serialized_task(
        self,
        target: RuntimeTarget,
        task_id: str,
        run: Callable[[], Awaitable[TurnOutcome]],
    ) -> TurnOutcome:
        self.requests.append((target, task_id))
        return await run()


@dataclass
class _Deps:
    workspaces: dict[str, WorkspaceProfile]
    queue: _Queue


def _workspace() -> WorkspaceProfile:
    return WorkspaceProfile(
        jid="slack:group",
        name="Group",
        folder="group",
        trigger="!p",
    )


def _turn(
    *,
    work_kind: InFlightWorkKind = InFlightWorkKind.INTERACTIVE,
    control_state: CheckpointControlState = CheckpointControlState.ACTIVE,
    task_id: str | None = None,
) -> InFlightTurn:
    return InFlightTurn(
        turn_id="turn-1",
        chat_jid="slack:group",
        group_folder="group",
        work_kind=work_kind,
        input_messages=[{"content": "resume"}],
        input_start_cursor="start",
        input_end_cursor="end",
        started_at="2026-07-28T20:00:00+00:00",
        control_state=control_state,
        task_id=task_id,
    )


def _task() -> ScheduledTask:
    return ScheduledTask(
        id="task-1",
        group_folder="group",
        chat_jid="slack:group",
        prompt="resume work",
        schedule_type="once",
        schedule_value="2026-07-29T20:00:00+00:00",
        session_policy=SessionPolicy.CONTINUE,
    )


def _deps(*, with_workspace: bool = True) -> _Deps:
    workspace = _workspace()
    return _Deps(workspaces={workspace.jid: workspace} if with_workspace else {}, queue=_Queue())


def _returning_turn(turn: InFlightTurn | None) -> Callable[[str], Awaitable[InFlightTurn | None]]:
    async def get_turn(_turn_id: str) -> InFlightTurn | None:
        await asyncio.sleep(0)
        return turn

    return get_turn


def _release_recorder(released: list[str]) -> Callable[[str], Awaitable[None]]:
    async def release(turn_id: str) -> None:
        await asyncio.sleep(0)
        released.append(turn_id)

    return release


def _returning_task(task: ScheduledTask | None) -> Callable[[str], Awaitable[ScheduledTask | None]]:
    async def get_task(_task_id: str) -> ScheduledTask | None:
        await asyncio.sleep(0)
        return task

    return get_task


@pytest.mark.asyncio
async def test_public_recovery_completes_when_checkpoint_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pynchy.host.orchestrator.interrupted_turns.get_in_flight_turn", _returning_turn(None)
    )

    assert await dispatch_interrupted_turn("turn-1", _deps()) is TurnOutcome.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("control_state", "expected"),
    [
        pytest.param(CheckpointControlState.PAUSE_REQUESTED, TurnOutcome.PAUSED),
        pytest.param(CheckpointControlState.PAUSED, TurnOutcome.PAUSED),
        pytest.param(CheckpointControlState.RESET_REQUESTED, TurnOutcome.RESET),
    ],
)
async def test_public_recovery_honors_durable_control_state(
    monkeypatch: pytest.MonkeyPatch,
    control_state: CheckpointControlState,
    expected: TurnOutcome,
) -> None:
    monkeypatch.setattr(
        "pynchy.host.orchestrator.interrupted_turns.get_in_flight_turn",
        _returning_turn(_turn(control_state=control_state)),
    )

    assert await dispatch_interrupted_turn("turn-1", _deps()) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_id", "task"),
    [
        pytest.param(None, None, id="missing-task-id"),
        pytest.param("task-1", None, id="deleted-task"),
    ],
)
async def test_public_recovery_releases_unrecoverable_scheduled_turn_claim(
    monkeypatch: pytest.MonkeyPatch,
    task_id: str | None,
    task: ScheduledTask | None,
) -> None:
    released: list[str] = []
    monkeypatch.setattr(
        "pynchy.host.orchestrator.interrupted_turns.get_in_flight_turn",
        _returning_turn(_turn(work_kind=InFlightWorkKind.SCHEDULED, task_id=task_id)),
    )
    monkeypatch.setattr(
        "pynchy.host.orchestrator.interrupted_turns.get_task_by_id", _returning_task(task)
    )
    monkeypatch.setattr(
        "pynchy.host.orchestrator.interrupted_turns.release_in_flight_turn_claim",
        _release_recorder(released),
    )

    with pytest.raises(RuntimeError, match="Interrupted scheduled"):
        await dispatch_interrupted_turn("turn-1", _deps())

    assert released == ["turn-1"]


@pytest.mark.asyncio
async def test_public_recovery_releases_scheduled_turn_when_workspace_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[str] = []
    monkeypatch.setattr(
        "pynchy.host.orchestrator.interrupted_turns.get_in_flight_turn",
        _returning_turn(_turn(work_kind=InFlightWorkKind.SCHEDULED, task_id="task-1")),
    )
    monkeypatch.setattr(
        "pynchy.host.orchestrator.interrupted_turns.get_task_by_id", _returning_task(_task())
    )
    monkeypatch.setattr(
        "pynchy.host.orchestrator.interrupted_turns.release_in_flight_turn_claim",
        _release_recorder(released),
    )

    with pytest.raises(RuntimeError, match="scheduled runtime no longer exists"):
        await dispatch_interrupted_turn("turn-1", _deps(with_workspace=False))

    assert released == ["turn-1"]


@pytest.mark.asyncio
async def test_public_recovery_serializes_scheduled_turn_in_resolved_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps = _deps()
    resumed: list[tuple[ScheduledTask, InFlightTurn]] = []

    async def resume_scheduled_turn(
        task: ScheduledTask, _deps: object, turn: InFlightTurn, _group: WorkspaceProfile
    ) -> TurnOutcome:
        await asyncio.sleep(0)
        resumed.append((task, turn))
        return TurnOutcome.COMPLETED

    monkeypatch.setattr(
        "pynchy.host.orchestrator.interrupted_turns.get_in_flight_turn",
        _returning_turn(_turn(work_kind=InFlightWorkKind.SCHEDULED, task_id="task-1")),
    )
    monkeypatch.setattr(
        "pynchy.host.orchestrator.interrupted_turns.get_task_by_id", _returning_task(_task())
    )
    monkeypatch.setattr(
        "pynchy.host.orchestrator.interrupted_turns.resume_interrupted_scheduled_turn",
        resume_scheduled_turn,
    )

    assert await dispatch_interrupted_turn("turn-1", deps) is TurnOutcome.COMPLETED
    assert deps.queue.requests == [(RuntimeTarget.from_workspace(_workspace()), "recovery:turn-1")]
    assert resumed == [(_task(), _turn(work_kind=InFlightWorkKind.SCHEDULED, task_id="task-1"))]


@pytest.mark.asyncio
async def test_public_recovery_releases_interactive_turn_when_workspace_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[str] = []
    monkeypatch.setattr(
        "pynchy.host.orchestrator.interrupted_turns.get_in_flight_turn", _returning_turn(_turn())
    )
    monkeypatch.setattr(
        "pynchy.host.orchestrator.interrupted_turns.release_in_flight_turn_claim",
        _release_recorder(released),
    )

    with pytest.raises(RuntimeError, match="Interrupted turn runtime no longer exists"):
        await dispatch_interrupted_turn("turn-1", _deps(with_workspace=False))

    assert released == ["turn-1"]


@pytest.mark.asyncio
async def test_public_recovery_serializes_interactive_turn_in_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps = _deps()
    resumed: list[InFlightTurn] = []

    async def resume_message_turn(
        _deps: object,
        _target: RuntimeTarget,
        _group: WorkspaceProfile,
        turn: InFlightTurn,
        _process_pending: object,
    ) -> TurnOutcome:
        await asyncio.sleep(0)
        resumed.append(turn)
        return TurnOutcome.RETRY

    monkeypatch.setattr(
        "pynchy.host.orchestrator.interrupted_turns.get_in_flight_turn", _returning_turn(_turn())
    )
    monkeypatch.setattr(
        "pynchy.host.orchestrator.interrupted_turns.resume_interrupted_message_turn",
        resume_message_turn,
    )

    assert await dispatch_interrupted_turn("turn-1", deps) is TurnOutcome.RETRY
    assert deps.queue.requests == [(RuntimeTarget.from_workspace(_workspace()), "recovery:turn-1")]
    assert resumed == [_turn()]
