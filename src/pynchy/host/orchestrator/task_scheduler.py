"""Schedule reconciler for Temporal-owned Pynchy orchestration."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 - beartype resolves scheduler annotations.
from typing import Any, Protocol, cast, runtime_checkable

from temporalio import activity

from pynchy.agent_protocol.api import (
    CheckpointControlState,
    InFlightTurn,
)
from pynchy.host.orchestrator.config_job_execution import (
    ConfigJobExecutionDeps,  # beartype resolves scheduler annotations.
    prepare_config_job,
    run_deterministic_config_job,
)
from pynchy.host.orchestrator.messaging.cursor import complete_turn_with_cursor
from pynchy.host.orchestrator.pipeline_review import (
    PipelineReviewHostDeps,
    run_configured_pipeline_reviews,
)
from pynchy.host.orchestrator.scheduled_binding import resolve_scheduled_group
from pynchy.host.orchestrator.scheduled_completion import classify_scheduled_agent_outcome
from pynchy.host.orchestrator.scheduled_failure_policy import (
    NO_PROGRESS_THRESHOLD,
    error_signature,
    recent_failure_run,
    scheduled_failure_decision,
)
from pynchy.host.orchestrator.scheduled_session_policy import apply_scheduled_session_policy
from pynchy.host.orchestrator.scheduled_turn import (
    SCHEDULED_TURN_INTERRUPTED,
    ScheduledTurnDeps,
    TaskAgentRequest,
    run_task_agent,
)
from pynchy.host.orchestrator.scheduler_deps import (  # noqa: TC001 - public runtime re-export.
    SchedulerDependencies,
)
from pynchy.host.orchestrator.temporal.api import (
    TemporalActivityInfo,
    get_temporal_scheduler_runtime,
    parse_temporal_activity_info,
)
from pynchy.logger import logger
from pynchy.plugins.api import (
    OutboundEvent,
    OutboundEventType,
)
from pynchy.scheduling.api import (
    ScheduledTask,
    TaskRunLog,
)
from pynchy.state.api import (
    claim_in_flight_turn,
    clear_chat_pause,
    clear_in_flight_turn,
    get_in_flight_turn_for_task,
    get_task_run_logs,
    is_chat_paused,
    log_task_run,
    record_task_completion,
    update_task,
)
from pynchy.turn_outcomes import TurnOutcome
from pynchy.workspace.api import (
    WorkspaceProfile,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)

_task_run_locks: dict[str, asyncio.Lock] = {}
TemporalSchedulerRuntime: Any | None = None

_scheduler_startup_lock = asyncio.Lock()
_scheduler_running = False


@runtime_checkable
class TemporalRuntime(Protocol):
    async def reconcile_schedules(self) -> None: ...


@runtime_checkable
class _SchedulerRuntimeDeps(Protocol):
    @property
    def scheduler_runtime(self) -> object: ...


def _build_temporal_runtime(deps: _SchedulerRuntimeDeps) -> object:
    """Build the Temporal runtime lazily to avoid a scheduler module import cycle."""
    runtime_cls = TemporalSchedulerRuntime
    if runtime_cls is None:
        runtime_cls = cast("Any", get_temporal_scheduler_runtime())
    return runtime_cls(deps, deps.scheduler_runtime)


async def _run_scheduler_owner(
    deps: _SchedulerRuntimeDeps,
    ready: asyncio.Future[None] | None,
) -> None:
    async with _build_temporal_runtime(deps) as temporal_runtime:
        logger.info("Scheduler loop started", backend="temporal")
        if ready is not None and not ready.done():
            ready.set_result(None)
        await _run_scheduler_loop(deps, temporal_runtime)


async def start_scheduler_loop(
    deps: _SchedulerRuntimeDeps,
    *,
    ready: asyncio.Future[None] | None = None,
) -> None:
    """Start the scheduler polling loop."""
    global _scheduler_running  # noqa: PLW0603 - process-wide Temporal queue owner.
    async with _scheduler_startup_lock:
        if _scheduler_running:
            raise RuntimeError("Temporal scheduler runtime is already running")
        _scheduler_running = True

    try:
        await _run_scheduler_owner(deps, ready)
    except BaseException as exc:
        if ready is not None and not ready.done():
            ready.set_exception(exc)
        raise
    finally:
        async with _scheduler_startup_lock:
            _scheduler_running = False


async def _run_scheduler_loop(
    _deps: _SchedulerRuntimeDeps, temporal_runtime: TemporalRuntime
) -> None:
    """Reconcile desired scheduled work into Temporal-owned schedules."""
    while True:
        if _deps.scheduler_runtime.reconcile_schedules:
            try:
                await temporal_runtime.reconcile_schedules()
            except Exception:  # noqa: BLE001 - scheduler loop is a long-lived reconcile boundary.
                logger.exception("Error in scheduler loop")

        await asyncio.sleep(_deps.scheduler_runtime.poll_interval)


def _temporal_run_metadata() -> TemporalActivityInfo | None:
    try:
        return parse_temporal_activity_info(activity.info())
    except RuntimeError:
        return None


async def _log_task_error(
    task_id: str,
    *,
    start_time: datetime,
    error: str,
    escalation_reason: str | None = None,
) -> None:
    temporal = _temporal_run_metadata()
    await log_task_run(
        TaskRunLog(
            task_id=task_id,
            run_at=datetime.now(UTC).isoformat(),
            duration_ms=(datetime.now(UTC) - start_time).total_seconds() * 1000,
            status="error",
            result=None,
            error=error,
            temporal_workflow_id=temporal.workflow_id if temporal else None,
            temporal_workflow_run_id=temporal.workflow_run_id if temporal else None,
            temporal_attempt=temporal.attempt if temporal else None,
            escalation_reason=escalation_reason,
        )
    )


async def _pause_task_for_circuit_breaker(
    task_id: str,
    *,
    start_time: datetime,
    trigger: str,
    reason: str,
) -> None:
    await update_task(task_id, {"status": "paused"})
    await _log_task_error(
        task_id,
        start_time=start_time,
        error=reason,
        escalation_reason=trigger,
    )


async def _broadcast_task_start(deps: SchedulerDependencies, chat_jid: str) -> None:
    await deps.broadcast_to_channels(
        chat_jid,
        OutboundEvent(type=OutboundEventType.SYSTEM, content="\u23f1 Scheduled task starting."),
    )


async def _scheduled_task_circuit_breaker(task_id: str) -> tuple[str, str] | None:
    logs = await get_task_run_logs(task_id, limit=NO_PROGRESS_THRESHOLD)
    return scheduled_failure_decision(recent_failure_run(logs))


async def _clear_pause_for_recurring_occurrence(task: ScheduledTask, chat_jid: str) -> None:
    if task.schedule_type != "once":
        await clear_chat_pause(chat_jid)


async def _finish_scheduled_agent_run(  # noqa: PLR0913 - scheduler completion needs its task, dependency port, and run record.
    task: ScheduledTask,
    deps: SchedulerDependencies,
    *,
    start_time: datetime,
    result: str | None,
    error: str | None,
    turn_id: str | None = None,
) -> TurnOutcome:
    outcome = await classify_scheduled_agent_outcome(deps, task.id, result=result, error=error)
    logger.info(
        "Task completed",
        task_id=task.id,
        run_status=outcome.status,
        duration_ms=(datetime.now(UTC) - start_time).total_seconds() * 1000,
    )
    duration_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000
    temporal = _temporal_run_metadata()
    await log_task_run(
        TaskRunLog(
            task_id=task.id,
            run_at=datetime.now(UTC).isoformat(),
            duration_ms=duration_ms,
            status=outcome.status,
            result=result,
            error=error,
            temporal_workflow_id=temporal.workflow_id if temporal else None,
            temporal_workflow_run_id=temporal.workflow_run_id if temporal else None,
            temporal_attempt=temporal.attempt if temporal else None,
            turn_id=turn_id,
            error_signature=error_signature(error) if error else None,
        )
    )
    await record_task_completion(
        task.id,
        last_result=outcome.summary,
        # A failed one-shot remains active so Temporal's activity retry can
        # run the same durable task. A clean but domain-incomplete occurrence
        # completes so bounded Linear reconciliation, not Temporal, retries it.
        completed=task.schedule_type == "once" and error is None,
    )
    return TurnOutcome.COMPLETED if error is None else TurnOutcome.RETRY


async def resume_interrupted_scheduled_turn(
    task: ScheduledTask,
    deps: SchedulerDependencies,
    turn: InFlightTurn,
    group: WorkspaceProfile,
    automation_memory_dir: Path | None = None,
) -> TurnOutcome:
    """Resume a claimed scheduled agent turn and finish its scheduler bookkeeping."""
    try:
        start_time = datetime.fromisoformat(turn.started_at)
    except ValueError:
        start_time = datetime.now(UTC)
    agent_run = await run_task_agent(
        TaskAgentRequest(
            task=task,
            deps=cast("ScheduledTurnDeps", deps),
            group=group,
            idle_timeout=deps.scheduler_runtime.idle_timeout,
            automation_memory_dir=automation_memory_dir,
            resume_turn=turn,
        )
    )
    if agent_run.terminal_outcome is not None:
        return agent_run.terminal_outcome
    result, error = await run_configured_pipeline_reviews(
        task,
        cast("PipelineReviewHostDeps", deps),
        group,
        result=agent_run.result,
        error=agent_run.error,
    )
    outcome = await _finish_scheduled_agent_run(
        task,
        deps,
        start_time=start_time,
        result=result,
        error=error,
        turn_id=agent_run.turn_id,
    )
    if outcome is TurnOutcome.COMPLETED:
        if turn.input_end_cursor and turn.chat_jid == group.jid:
            await complete_turn_with_cursor(
                deps,
                group.jid,
                turn.input_end_cursor,
                turn.turn_id,
                conversation_claim_id=turn.conversation_claim_id,
            )
        else:
            await clear_in_flight_turn(turn.turn_id)
    return outcome


async def run_scheduled_agent(
    task: ScheduledTask,
    deps: SchedulerDependencies,
    *,
    occurrence_id: str | None = None,
) -> TurnOutcome:
    """Execute a single scheduled agent task via the unified run_agent path."""
    if (
        task.schedule_type == "once"
        and task.bound_chat_jid is not None
        and await is_chat_paused(task.bound_chat_jid)
    ):
        return TurnOutcome.PAUSED
    # Temporal BUFFER_ONE serializes normal schedule overlap durably. This
    # process-local lock also serializes duplicate/manual activity delivery so
    # every execution stays in the task's one memory directory and derived thread.
    lock = _task_run_locks.setdefault(task.id, asyncio.Lock())
    async with lock:
        memory_context = (
            deps.automation_memory_dir(task.id) if task.memory_enabled else nullcontext(None)
        )
        with memory_context as memory_dir:
            return await _run_scheduled_agent(
                task,
                deps,
                occurrence_id=occurrence_id,
                automation_memory_dir=memory_dir,
            )


async def _run_scheduled_agent(  # noqa: PLR0911 - explicit scheduler terminal outcomes.
    task: ScheduledTask,
    deps: SchedulerDependencies,
    *,
    occurrence_id: str | None,
    automation_memory_dir: Path | None,
) -> TurnOutcome:
    """Run one task after applying config-job serialization."""
    start_time = datetime.now(UTC)
    if task.config_job_name is not None and task.config_job_is_deterministic is None:
        await _log_task_error(
            task.id,
            start_time=start_time,
            error="Config job execution is awaiting reconciliation",
        )
        return TurnOutcome.RETRY
    interrupted_turn = await get_in_flight_turn_for_task(task.id)
    if interrupted_turn is not None and interrupted_turn.control_state in {
        CheckpointControlState.PAUSE_REQUESTED,
        CheckpointControlState.PAUSED,
    }:
        return TurnOutcome.PAUSED
    if (
        interrupted_turn is not None
        and interrupted_turn.control_state is CheckpointControlState.RESET_REQUESTED
    ):
        await clear_in_flight_turn(interrupted_turn.turn_id)
        return TurnOutcome.RESET

    runtime_folder = task.bound_group_folder
    runtime_jid = task.bound_chat_jid
    if runtime_folder is None or runtime_jid is None:
        await _log_task_error(
            task.id,
            start_time=start_time,
            error="Scheduled task has no durable conversation binding",
        )
        return TurnOutcome.RETRY
    group = resolve_scheduled_group(deps.workspaces, runtime_folder)
    if group is None or group.jid != runtime_jid:
        await _log_task_error(
            task.id,
            start_time=start_time,
            error=f"Scheduled task runtime is not registered: {runtime_folder}",
        )
        return TurnOutcome.RETRY
    if interrupted_turn is not None:
        if not await claim_in_flight_turn(interrupted_turn.turn_id):
            logger.info(
                "Interrupted scheduled turn already claimed",
                task_id=task.id,
                turn_id=interrupted_turn.turn_id,
            )
            return TurnOutcome.COMPLETED
        return await resume_interrupted_scheduled_turn(
            task,
            deps,
            interrupted_turn,
            group,
            automation_memory_dir,
        )

    resolved_occurrence = occurrence_id or f"direct:{task.id}:{start_time.isoformat()}"
    task = await apply_scheduled_session_policy(
        task,
        group,
        resolved_occurrence,
        deps.reset_scheduled_context,
        update_task,
    )
    group_dir = deps.scheduler_runtime.groups_dir / runtime_folder
    group_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Running scheduled task", task_id=task.id, group=runtime_folder)

    circuit_decision = await _scheduled_task_circuit_breaker(task.id)
    if circuit_decision is not None:
        trigger, reason = circuit_decision
        await _pause_task_for_circuit_breaker(
            task.id,
            start_time=start_time,
            trigger=trigger,
            reason=reason,
        )
        logger.warning("Scheduled task paused by circuit breaker", task_id=task.id, reason=reason)
        return TurnOutcome.PAUSED

    await _clear_pause_for_recurring_occurrence(task, runtime_jid)

    deterministic = await run_deterministic_config_job(
        task,
        cast("ConfigJobExecutionDeps", deps),
        automation_memory_dir,
    )
    if deterministic is not None:
        return await _finish_scheduled_agent_run(
            task,
            deps,
            start_time=start_time,
            result=deterministic.result,
            error=deterministic.error,
        )

    prepared_task, skipped_result = await prepare_config_job(task, automation_memory_dir)
    if prepared_task is None:
        return await _finish_scheduled_agent_run(
            task,
            deps,
            start_time=start_time,
            result=skipped_result,
            error=None,
        )

    execution_task = prepared_task

    async def on_started(chat_jid: str) -> None:
        await _broadcast_task_start(deps, chat_jid)

    agent_run = await run_task_agent(
        TaskAgentRequest(
            task=execution_task,
            deps=cast("ScheduledTurnDeps", deps),
            group=group,
            idle_timeout=deps.scheduler_runtime.idle_timeout,
            automation_memory_dir=automation_memory_dir,
            on_started=on_started,
        )
    )
    if agent_run.terminal_outcome is not None:
        return agent_run.terminal_outcome
    if agent_run.error == SCHEDULED_TURN_INTERRUPTED:
        logger.info(
            "Scheduled task yielded to an interactive turn",
            task_id=task.id,
            turn_id=agent_run.turn_id,
        )
        return TurnOutcome.RETRY
    result, error = await run_configured_pipeline_reviews(
        task,
        cast("PipelineReviewHostDeps", deps),
        group,
        result=agent_run.result,
        error=agent_run.error,
    )
    return await _finish_scheduled_agent_run(
        task,
        deps,
        start_time=start_time,
        result=result,
        error=error,
        turn_id=agent_run.turn_id,
    )
