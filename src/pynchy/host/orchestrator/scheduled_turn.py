"""Agent invocation and checkpoint lifecycle for scheduled turns."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pynchy.conversation.events import new_turn_id
from pynchy.host.container_manager import OnOutput  # noqa: TC001 - beartype resolves Protocols.
from pynchy.host.git_ops._worktree_merge import merge_worktree_with_policy
from pynchy.host.orchestrator.messaging.in_flight import (
    MessageTurnStart,
    begin_message_turn,
    interrupted_resume_message,
)
from pynchy.logger import logger
from pynchy.state import (
    clear_in_flight_turn,
    mark_in_flight_output_sent,
    release_in_flight_turn_claim,
)
from pynchy.types import (
    ContainerOutput,
    InFlightTurn,
    InFlightWorkKind,
    ScheduledTask,
    WorkspaceProfile,
)
from pynchy.utils import IdleTimer


@runtime_checkable
class ScheduledTurnQueue(Protocol):
    def close_stdin(self, chat_jid: str) -> None: ...


@runtime_checkable
class ScheduledTurnDeps(Protocol):
    @property
    def queue(self) -> ScheduledTurnQueue: ...

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


@dataclass
class _TaskAgentStreamState:
    result: str | None = None
    error: str | None = None
    output_sent: bool = False


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


def _scheduled_idle_timer(request: TaskAgentRequest) -> IdleTimer | None:
    if not request.idle_enabled:
        return None

    def _idle_timeout_callback() -> None:
        logger.debug("Scheduled task idle timeout, closing stdin", task_id=request.task.id)
        request.deps.queue.close_stdin(request.task.chat_jid)

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


def _task_output_handler(
    request: TaskAgentRequest,
    state: _TaskAgentStreamState,
    turn_id: str,
    idle_timer: IdleTimer | None,
) -> OnOutput:
    async def _on_output(streamed: ContainerOutput) -> None:
        sent = await request.deps.handle_streamed_output(
            request.task.chat_jid,
            request.group,
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
    if resume_turn is None:
        await begin_message_turn(
            MessageTurnStart(
                turn_id=turn_id,
                chat_jid=request.task.chat_jid,
                group=request.group,
                work_kind=InFlightWorkKind.SCHEDULED,
                input_messages=input_messages,
                input_start_cursor="",
                input_end_cursor="",
                task_id=request.task.id,
            )
        )
    state = _TaskAgentStreamState(output_sent=resume_turn.output_sent if resume_turn else False)
    interrupted = False
    idle_timer = _scheduled_idle_timer(request)
    on_output = _task_output_handler(request, state, turn_id, idle_timer)
    if idle_timer:
        idle_timer.reset()

    try:
        agent_result = await request.deps.run_agent(
            request.group,
            request.task.chat_jid,
            input_messages,
            on_output,
            is_scheduled_task=True,
            repo_access_override=None,
            input_source=request.task.input_source,
            turn_id=turn_id,
        )
        if agent_result == "error":
            state.error = state.error or "Agent returned error"
        await _merge_scheduled_task_worktree(request.task, error=state.error)
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
        if idle_timer:
            idle_timer.cancel()
        await _finish_checkpoint(
            turn_id,
            resume_turn,
            interrupted=interrupted,
            error=state.error,
        )
