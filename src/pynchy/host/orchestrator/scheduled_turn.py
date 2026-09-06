"""Agent invocation and checkpoint lifecycle for scheduled turns."""

from __future__ import annotations

import asyncio
from collections.abc import (
    Awaitable,
    Callable,
    Iterator,
)
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pynchy.agent_protocol.api import (
    CheckpointControlState,
    ContainerOutput,
    InFlightTurn,
    InFlightWorkKind,
    OnOutput,
)
from pynchy.conversation.api import new_turn_id
from pynchy.host.orchestrator.messaging.in_flight import (
    MessageTurnStart,
    begin_message_turn,
    requested_control_outcome,
    semantic_resume_messages,
)
from pynchy.host.orchestrator.scheduled_turn_deps import (
    ScheduledTurnDeps,
)
from pynchy.identifiers import RuntimeId
from pynchy.logger import logger
from pynchy.scheduling.api import (
    ScheduledTask,
)
from pynchy.state.api import (
    clear_in_flight_turn,
    get_in_flight_turn_for_task,
    get_task_by_id,
    mark_in_flight_output_sent,
    release_in_flight_turn_claim,
)
from pynchy.turn_outcomes import (
    TurnOutcome,
)
from pynchy.workspace.api import (
    WorkspaceProfile,
)

SCHEDULED_TURN_INTERRUPTED = "__scheduled_turn_interrupted__"


@dataclass(frozen=True)
class TaskAgentRequest:
    task: ScheduledTask
    deps: ScheduledTurnDeps
    group: WorkspaceProfile
    idle_timeout: float
    automation_memory_dir: Path | None = None
    resume_turn: InFlightTurn | None = None
    on_started: Callable[[str], Awaitable[None]] | None = None


@dataclass
class _TaskAgentStreamState:
    result: str | None = None
    error: str | None = None
    output_sent: bool = False
    terminal_outcome: TurnOutcome | None = None


@dataclass(frozen=True)
class TaskAgentResult:
    """Result of one scheduled agent invocation and its checkpoint transition."""

    turn_id: str
    result: str | None
    error: str | None
    terminal_outcome: TurnOutcome | None = None


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


async def pause_queued_once_task(
    task_id: str,
    group: WorkspaceProfile,
    chat_jid: str,
) -> bool:
    """Freeze queued one-shot work before its queue slot can start."""
    task = await get_task_by_id(task_id)
    if (
        task is None
        or task.status != "active"
        or task.schedule_type != "once"
        or task.bound_chat_jid != chat_jid
        or task.bound_group_folder != group.folder
        or await get_in_flight_turn_for_task(task_id) is not None
    ):
        return False
    await begin_message_turn(
        MessageTurnStart(
            turn_id=new_turn_id(),
            chat_jid=chat_jid,
            group=group,
            work_kind=InFlightWorkKind.SCHEDULED,
            input_messages=[_scheduled_task_message(task)],
            input_start_cursor="",
            input_end_cursor="",
            task_id=task.id,
            input_source=task.input_source,
            control_state=CheckpointControlState.PAUSED,
        )
    )
    return True


@contextmanager
def _scheduled_idle_timer(request: TaskAgentRequest) -> Iterator[Callable[[], None]]:
    """Close idle stdin and cancel the timer when this invocation exits."""

    def _idle_timeout_callback() -> None:
        logger.debug("Scheduled task idle timeout, closing stdin", task_id=request.task.id)
        request.deps.queue.close_stdin(RuntimeId(request.group.folder))

    loop = asyncio.get_running_loop()
    handle = loop.call_later(request.idle_timeout, _idle_timeout_callback)

    def reset() -> None:
        nonlocal handle
        handle.cancel()
        handle = loop.call_later(request.idle_timeout, _idle_timeout_callback)

    try:
        yield reset
    finally:
        handle.cancel()


async def _checkpoint_new_task(
    request: TaskAgentRequest,
    turn_id: str,
    input_messages: list[dict[str, Any]],
) -> None:
    """Checkpoint one occurrence in its already-bound durable runtime."""
    if (
        request.task.bound_chat_jid != request.group.jid
        or request.task.bound_group_folder != request.group.folder
    ):
        raise RuntimeError("Scheduled task runtime binding does not match its queue owner")
    await begin_message_turn(
        MessageTurnStart(
            turn_id=turn_id,
            chat_jid=request.group.jid,
            group=request.group,
            work_kind=InFlightWorkKind.SCHEDULED,
            input_messages=input_messages,
            input_start_cursor="",
            input_end_cursor="",
            task_id=request.task.id,
            input_source=request.task.input_source,
        )
    )


def _task_output_handler(
    request: TaskAgentRequest,
    state: _TaskAgentStreamState,
    turn_id: str,
    reset_idle_timer: Callable[[], None],
) -> OnOutput:
    async def _on_output(streamed: ContainerOutput) -> None:
        sent = await request.deps.handle_streamed_output(
            request.group.jid,
            request.group,
            streamed,
            turn_id=turn_id,
        )
        if sent and not state.output_sent:
            state.output_sent = True
            await mark_in_flight_output_sent(turn_id)
        reset_idle_timer()
        if streamed.result:
            state.result = streamed.result
        if streamed.status == "error":
            state.error = streamed.error or "Unknown error"
        if streamed.type == "tool_result":
            await request.deps.queue.interrupt_after_tool_result(RuntimeId(request.group.folder))

    return _on_output


async def _run_target_agent(
    request: TaskAgentRequest,
    state: _TaskAgentStreamState,
    turn_id: str,
    input_messages: list[dict[str, Any]],
) -> None:
    if request.on_started is not None and request.resume_turn is None:
        await request.on_started(request.group.jid)
    with _scheduled_idle_timer(request) as reset_idle_timer:
        on_output = _task_output_handler(request, state, turn_id, reset_idle_timer)
        agent_result = await request.deps.run_agent(
            request.group,
            request.group.jid,
            input_messages,
            on_output,
            extra_system_notices=(
                [
                    (
                        "Persistent task memory is the directory named by "
                        "PYNCHY_AUTOMATION_MEMORY_DIR. Read it before acting and update it "
                        "only with durable state needed by a future occurrence."
                    )
                ]
                if request.automation_memory_dir is not None
                else None
            ),
            is_scheduled_task=True,
            repo_access_override=request.task.repo_access,
            input_source=(
                request.resume_turn.input_source
                if request.resume_turn is not None
                else request.task.input_source
            ),
            turn_id=turn_id,
            resume_session_id=request.resume_turn.session_id if request.resume_turn else None,
            automation_memory_dir=request.automation_memory_dir,
        )
        state.terminal_outcome = await requested_control_outcome(
            turn_id,
            agent_succeeded=agent_result == "success" and state.error is None,
        )
        if state.terminal_outcome is not None:
            state.error = None
            return
        if request.deps.queue.boundary_interrupt_requested(RuntimeId(request.group.folder)):
            state.error = SCHEDULED_TURN_INTERRUPTED
        elif agent_result == "error":
            state.error = state.error or "Agent returned error"


async def _finish_checkpoint(
    turn_id: str,
    resume_turn: InFlightTurn | None,
    *,
    interrupted: bool,
    error: str | None,
    terminal_outcome: TurnOutcome | None,
) -> None:
    if interrupted or terminal_outcome is not None:
        return
    if error:
        # Keep the checkpoint for a first attempt as well as a resumed one.
        # Temporal retries the activity after an agent failure; preserving its
        # selected target prevents a retry from creating another child thread.
        await release_in_flight_turn_claim(turn_id)
        return
    if resume_turn is None:
        await clear_in_flight_turn(turn_id)


async def run_task_agent(request: TaskAgentRequest) -> TaskAgentResult:
    """Run or semantically resume one checkpointed scheduled agent invocation."""
    resume_turn = request.resume_turn
    turn_id = resume_turn.turn_id if resume_turn else new_turn_id()
    input_messages = (
        semantic_resume_messages(resume_turn)
        if resume_turn
        else [_scheduled_task_message(request.task)]
    )
    state = _TaskAgentStreamState(output_sent=resume_turn.output_sent if resume_turn else False)
    interrupted = False
    checkpoint_started = resume_turn is not None

    try:
        if resume_turn is None:
            await _checkpoint_new_task(request, turn_id, input_messages)
        checkpoint_started = True
        await _run_target_agent(request, state, turn_id, input_messages)
    except asyncio.CancelledError:
        interrupted = True
        raise
    except Exception as exc:  # noqa: BLE001 - agent invocation returns task errors.
        state.terminal_outcome = await requested_control_outcome(
            turn_id,
            agent_succeeded=False,
        )
        logger.info(
            "Scheduled agent process exited",
            task_id=request.task.id,
            controlled=state.terminal_outcome is not None,
        )
        if state.terminal_outcome is None:
            state.error = str(exc)
            logger.error("Task failed", task_id=request.task.id, error=state.error)
    finally:
        if checkpoint_started:
            await _finish_checkpoint(
                turn_id,
                resume_turn,
                interrupted=interrupted,
                error=state.error,
                terminal_outcome=state.terminal_outcome,
            )
    return TaskAgentResult(
        turn_id,
        state.result,
        state.error,
        terminal_outcome=state.terminal_outcome,
    )
