"""Agent invocation and checkpoint lifecycle for scheduled turns."""

from __future__ import annotations

import asyncio
from collections.abc import (  # noqa: TC003, RUF100 - beartype resolves these runtime annotations.
    Awaitable,
    Callable,
)
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pynchy.conversation.events import new_turn_id
from pynchy.host.container_manager import OnOutput, get_session
from pynchy.host.git_ops._worktree_merge import merge_worktree_with_policy
from pynchy.host.orchestrator.messaging.in_flight import (
    MessageTurnStart,
    begin_message_turn,
    interrupted_resume_message,
)
from pynchy.host.orchestrator.workspace_config import dynamic_thread_folder
from pynchy.logger import logger
from pynchy.state import (
    clear_in_flight_turn,
    get_in_flight_turns,
    mark_in_flight_output_sent,
    release_in_flight_turn_claim,
)
from pynchy.types import (
    ContainerOutput,
    GroupFolder,
    InFlightTurn,
    InFlightWorkKind,
    ScheduledTask,
    WorkspaceProfile,
)
from pynchy.utils import IdleTimer

_scheduled_target_lock = asyncio.Lock()


@runtime_checkable
class ScheduledTurnQueue(Protocol):
    def close_stdin(self, chat_jid: str) -> None: ...


@runtime_checkable
class ScheduledTurnDeps(Protocol):
    @property
    def queue(self) -> ScheduledTurnQueue: ...

    async def create_scheduled_thread(self, parent_jid: str, name: str) -> str: ...

    async def run_agent(  # noqa: PLR0913, RUF100 - mirrors the orchestrator contract.
        self,
        group: WorkspaceProfile,
        chat_jid: str,
        messages: list[dict[str, Any]],
        on_output: OnOutput | None = None,
        extra_system_notices: list[str] | None = None,
        *,
        is_scheduled_task: bool = False,
        repo_access_override: str | None = None,
        input_source: str = "user",
        turn_id: str | None = None,
    ) -> str: ...

    async def handle_streamed_output(
        self,
        chat_jid: str,
        group: WorkspaceProfile,
        result: ContainerOutput,
        *,
        turn_id: str | None = None,
    ) -> bool: ...


@dataclass(frozen=True)
class TaskAgentRequest:
    task: ScheduledTask
    deps: ScheduledTurnDeps
    group: WorkspaceProfile
    idle_enabled: bool
    idle_timeout: float
    resume_turn: InFlightTurn | None = None
    on_started: Callable[[ScheduledTask], Awaitable[None]] | None = None


@dataclass(frozen=True)
class _ScheduledTaskTarget:
    task: ScheduledTask
    group: WorkspaceProfile
    thread_slot: int


@dataclass
class _TaskAgentStreamState:
    result: str | None = None
    error: str | None = None
    output_sent: bool = False


@dataclass(frozen=True)
class _TargetAgentRun:
    request: TaskAgentRequest
    target: _ScheduledTaskTarget
    state: _TaskAgentStreamState
    turn_id: str
    input_messages: list[dict[str, Any]]
    is_new_turn: bool


def _scheduled_task_message(task: ScheduledTask) -> dict[str, Any]:
    sender_name = "Webhook" if task.input_source.startswith("webhook:") else "Scheduled Task"
    return {
        "message_type": "user",
        "sender": task.input_source,
        "sender_name": sender_name,
        "content": task.prompt,
        "timestamp": datetime.now(UTC).isoformat(),
        "metadata": {"source": task.input_source},
    }


def _scheduled_idle_timer(
    request: TaskAgentRequest,
    target: _ScheduledTaskTarget,
) -> IdleTimer | None:
    if not request.idle_enabled:
        return None

    def _idle_timeout_callback() -> None:
        logger.debug("Scheduled task idle timeout, closing stdin", task_id=request.task.id)
        request.deps.queue.close_stdin(target.task.chat_jid)

    return IdleTimer(request.idle_timeout, _idle_timeout_callback)


async def _merge_scheduled_task_worktree(task: ScheduledTask, *, error: str | None) -> None:
    if not error:
        await merge_worktree_with_policy(task.group_folder)


def _task_agent_messages(request: TaskAgentRequest) -> list[dict[str, Any]]:
    return (
        [interrupted_resume_message(request.resume_turn)]
        if request.resume_turn
        else [_scheduled_task_message(request.task)]
    )


def _parent_session_is_live(task: ScheduledTask) -> bool:
    session = get_session(GroupFolder(task.group_folder))
    return session is not None and session.is_alive


def _next_thread_slot(task: ScheduledTask, turns: list[InFlightTurn]) -> int:
    """Return the base slot or the first available numbered child slot."""
    base_reserved = _parent_session_is_live(task) or any(
        turn.chat_jid == task.chat_jid for turn in turns
    )
    occupied_slots = {
        turn.scheduled_thread_slot
        for turn in turns
        if turn.scheduled_base_chat_jid == task.chat_jid and turn.scheduled_thread_slot is not None
    }
    if not base_reserved and 0 not in occupied_slots:
        return 0

    slot = 1
    while slot in occupied_slots:
        slot += 1
    return slot


def _thread_name(task: ScheduledTask, slot: int) -> str:
    return f"{task.group_folder}-{slot}"


async def _new_task_target(
    request: TaskAgentRequest,
    turn_id: str,
    input_messages: list[dict[str, Any]],
) -> _ScheduledTaskTarget:
    """Claim an idle base chat or create a numbered child thread for this turn."""
    async with _scheduled_target_lock:
        slot = _next_thread_slot(request.task, await get_in_flight_turns())
        if slot == 0:
            target = _ScheduledTaskTarget(request.task, request.group, slot)
        else:
            name = _thread_name(request.task, slot)
            child_jid = await request.deps.create_scheduled_thread(request.task.chat_jid, name)
            if not child_jid:
                raise RuntimeError("Scheduled task thread creation returned no chat JID")
            child_group = replace(
                request.group,
                jid=child_jid,
                name=f"{request.group.name}/{name}",
                folder=dynamic_thread_folder(request.group.folder, child_jid),
            )
            target = _ScheduledTaskTarget(
                task=replace(request.task, group_folder=child_group.folder, chat_jid=child_jid),
                group=child_group,
                thread_slot=slot,
            )
        await begin_message_turn(
            MessageTurnStart(
                turn_id=turn_id,
                chat_jid=target.task.chat_jid,
                group=target.group,
                work_kind=InFlightWorkKind.SCHEDULED,
                input_messages=input_messages,
                input_start_cursor="",
                input_end_cursor="",
                task_id=request.task.id,
                scheduled_base_chat_jid=request.task.chat_jid,
                scheduled_thread_slot=slot,
            )
        )
        return target


def _resumed_task_target(request: TaskAgentRequest, turn: InFlightTurn) -> _ScheduledTaskTarget:
    """Rebuild the durable conversation assignment for an interrupted task."""
    target_group = replace(request.group, jid=turn.chat_jid, folder=turn.group_folder)
    target_task = replace(request.task, group_folder=turn.group_folder, chat_jid=turn.chat_jid)
    return _ScheduledTaskTarget(
        task=target_task,
        group=target_group,
        thread_slot=turn.scheduled_thread_slot or 0,
    )


def _task_output_handler(
    request: TaskAgentRequest,
    target: _ScheduledTaskTarget,
    state: _TaskAgentStreamState,
    turn_id: str,
    idle_timer: IdleTimer | None,
) -> OnOutput:
    async def _on_output(streamed: ContainerOutput) -> None:
        sent = await request.deps.handle_streamed_output(
            target.task.chat_jid,
            target.group,
            streamed,
            turn_id=turn_id,
        )
        if sent and not state.output_sent:
            state.output_sent = True
            await mark_in_flight_output_sent(turn_id)
        if idle_timer:
            idle_timer.reset()
        if streamed.result:
            state.result = streamed.result
        if streamed.status == "error":
            state.error = streamed.error or "Unknown error"

    return _on_output


async def _run_target_agent(run: _TargetAgentRun) -> None:
    request = run.request
    target = run.target
    state = run.state
    if request.on_started is not None and run.is_new_turn:
        await request.on_started(target.task)
    idle_timer = _scheduled_idle_timer(request, target)
    on_output = _task_output_handler(request, target, state, run.turn_id, idle_timer)
    if idle_timer:
        idle_timer.reset()
    try:
        agent_result = await request.deps.run_agent(
            target.group,
            target.task.chat_jid,
            run.input_messages,
            on_output,
            is_scheduled_task=True,
            repo_access_override=None,
            input_source=request.task.input_source,
            turn_id=run.turn_id,
        )
        if agent_result == "error":
            state.error = state.error or "Agent returned error"
        await _merge_scheduled_task_worktree(target.task, error=state.error)
    finally:
        if idle_timer:
            idle_timer.cancel()


async def _finish_checkpoint(
    turn_id: str,
    resume_turn: InFlightTurn | None,
    *,
    interrupted: bool,
    error: str | None,
) -> None:
    if interrupted:
        return
    if resume_turn is None:
        await clear_in_flight_turn(turn_id)
    elif error:
        await release_in_flight_turn_claim(turn_id)


async def run_task_agent(request: TaskAgentRequest) -> tuple[str | None, str | None]:
    """Run or semantically resume one checkpointed scheduled agent invocation."""
    resume_turn = request.resume_turn
    turn_id = resume_turn.turn_id if resume_turn else new_turn_id()
    input_messages = _task_agent_messages(request)
    state = _TaskAgentStreamState(output_sent=resume_turn.output_sent if resume_turn else False)
    interrupted = False
    checkpoint_started = resume_turn is not None

    try:
        target = (
            _resumed_task_target(request, resume_turn)
            if resume_turn is not None
            else await _new_task_target(request, turn_id, input_messages)
        )
        checkpoint_started = True
        await _run_target_agent(
            _TargetAgentRun(
                request=request,
                target=target,
                state=state,
                turn_id=turn_id,
                input_messages=input_messages,
                is_new_turn=resume_turn is None,
            ),
        )
    except asyncio.CancelledError:
        interrupted = True
        raise
    except Exception as exc:  # noqa: BLE001, RUF100 - agent invocation returns task errors.
        state.error = str(exc)
        logger.error("Task failed", task_id=request.task.id, error=state.error)
        return state.result, state.error
    else:
        return state.result, state.error
    finally:
        if checkpoint_started:
            await _finish_checkpoint(
                turn_id,
                resume_turn,
                interrupted=interrupted,
                error=state.error,
            )
