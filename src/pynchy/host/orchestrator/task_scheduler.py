"""Schedule reconciler for Temporal-owned Pynchy orchestration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

if TYPE_CHECKING:
    from pynchy.config.scheduler_models import SchedulerConfig
    from pynchy.host.orchestrator.concurrency import GroupQueue

from temporalio import activity

from pynchy.config import get_settings
from pynchy.host.container_manager import (  # noqa: TC001, RUF100 - beartype resolves runtime annotations.
    OnOutput,
)
from pynchy.host.orchestrator.config_job_execution import (
    ConfigJobExecutionDeps,  # noqa: TC001, RUF100 - beartype resolves scheduler annotations.
    prepare_config_job,
    run_deterministic_config_job,
)
from pynchy.host.orchestrator.scheduled_binding import resolve_scheduled_group
from pynchy.host.orchestrator.scheduled_circuit_breaker import (
    error_signature,
    scheduled_task_circuit_decision,
)
from pynchy.host.orchestrator.scheduled_completion import classify_scheduled_agent_outcome
from pynchy.host.orchestrator.scheduled_session_policy import apply_scheduled_session_policy
from pynchy.host.orchestrator.scheduled_turn import (
    SCHEDULED_TURN_INTERRUPTED,
    ScheduledTurnDeps,
    TaskAgentRequest,
    run_task_agent,
)
from pynchy.host.orchestrator.temporal.runtime_state import (
    TemporalActivityInfo,
    parse_temporal_activity_info,
)
from pynchy.logger import logger
from pynchy.state import (
    claim_in_flight_turn,
    clear_in_flight_turn,
    get_in_flight_turn_for_task,
    get_task_run_logs,
    log_task_run,
    record_task_completion,
    release_in_flight_turn_claim,
    update_task,
)
from pynchy.types import (
    ContainerOutput,
    InFlightTurn,
    OutboundEvent,
    OutboundEventType,
    ScheduledTask,
    TaskRunLog,
    WorkspaceProfile,
)


@runtime_checkable
class SchedulerDependencies(Protocol):
    """Dependencies for the task scheduler."""

    @property
    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    @property
    def queue(self) -> GroupQueue: ...

    async def broadcast_to_channels(self, jid: str, event: OutboundEvent) -> None: ...

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...

    async def broadcast_system_notice(self, chat_jid: str, text: str) -> None: ...

    async def reset_scheduled_context(
        self,
        task: ScheduledTask,
        group: WorkspaceProfile,
        occurrence_id: str,
    ) -> None: ...

    async def run_agent(  # noqa: PLR0913, RUF100 - scheduler protocol preserves the full agent execution contract.
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


_scheduler_lock = asyncio.Lock()
_config_job_run_locks: dict[str, asyncio.Lock] = {}
TemporalSchedulerRuntime: Any | None = None


@dataclass
class _SchedulerState:
    scheduler_running: bool = False


_state = _SchedulerState()


@runtime_checkable
class TemporalRuntime(Protocol):
    async def reconcile_schedules(self) -> None: ...


def _build_temporal_runtime(deps: SchedulerDependencies, scheduler_config: object) -> object:
    """Build the Temporal runtime lazily to avoid a scheduler module import cycle."""
    runtime_cls = TemporalSchedulerRuntime
    if runtime_cls is None:
        from pynchy.host.orchestrator.temporal.scheduler import (  # noqa: PLC0415, RUF100 - importing the Temporal scheduler at module load creates a cycle.
            TemporalSchedulerRuntime as _TemporalSchedulerRuntime,
        )

        runtime_cls = _TemporalSchedulerRuntime
    return runtime_cls(deps, cast("SchedulerConfig", scheduler_config))


async def start_scheduler_loop(deps: SchedulerDependencies) -> None:
    """Start the scheduler polling loop."""
    async with _scheduler_lock:
        if _state.scheduler_running:
            logger.debug("Scheduler loop already running, skipping duplicate start")
            return
        _state.scheduler_running = True

    try:
        scheduler_config = get_settings().scheduler
        logger.info("Scheduler loop started", backend="temporal")
        async with _build_temporal_runtime(deps, scheduler_config) as temporal_runtime:
            await _run_scheduler_loop(deps, temporal_runtime)
    finally:
        async with _scheduler_lock:
            _state.scheduler_running = False


async def _run_scheduler_loop(
    _deps: SchedulerDependencies, temporal_runtime: TemporalRuntime
) -> None:
    """Reconcile desired scheduled work into Temporal-owned schedules."""
    while True:
        try:
            await temporal_runtime.reconcile_schedules()
        except Exception:  # noqa: BLE001, RUF100 - scheduler loop is a long-lived reconcile boundary.
            logger.exception("Error in scheduler loop")

        await asyncio.sleep(get_settings().scheduler.poll_interval)


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


async def _log_missing_group(task: ScheduledTask, start_time: datetime) -> None:
    await _log_task_error(
        task.id,
        start_time=start_time,
        error=f"Group not found: {task.group_folder}",
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


async def _broadcast_task_start(deps: SchedulerDependencies, task: ScheduledTask) -> None:
    await deps.broadcast_to_channels(
        task.chat_jid,
        OutboundEvent(type=OutboundEventType.SYSTEM, content="\u23f1 Scheduled task starting."),
    )


async def _scheduled_task_circuit_breaker(task_id: str) -> tuple[str, str] | None:
    return scheduled_task_circuit_decision(await get_task_run_logs(task_id, limit=5))


async def _finish_scheduled_agent_run(
    task: ScheduledTask,
    *,
    start_time: datetime,
    result: str | None,
    error: str | None,
    turn_id: str | None = None,
) -> bool:
    outcome = await classify_scheduled_agent_outcome(task.id, result=result, error=error)
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
    return error is None


async def resume_interrupted_scheduled_turn(
    task: ScheduledTask,
    deps: SchedulerDependencies,
    turn: InFlightTurn,
) -> bool:
    """Resume a claimed scheduled agent turn and finish its scheduler bookkeeping."""
    group = (
        resolve_scheduled_group(deps.workspaces, task.bound_group_folder)
        if task.bound_group_folder is not None
        else None
    )
    if group is None:
        await release_in_flight_turn_claim(turn.turn_id)
        return False
    try:
        start_time = datetime.fromisoformat(turn.started_at)
    except ValueError:
        start_time = datetime.now(UTC)
    turn_id, result, error = await run_task_agent(
        TaskAgentRequest(
            task=task,
            deps=cast("ScheduledTurnDeps", deps),
            group=group,
            idle_enabled=True,
            idle_timeout=get_settings().idle_timeout,
            resume_turn=turn,
        )
    )
    completed = await _finish_scheduled_agent_run(
        task,
        start_time=start_time,
        result=result,
        error=error,
        turn_id=turn_id,
    )
    if completed:
        await clear_in_flight_turn(turn.turn_id)
    return completed


async def run_scheduled_agent(
    task: ScheduledTask,
    deps: SchedulerDependencies,
    *,
    occurrence_id: str | None = None,
) -> bool:
    """Execute a single scheduled agent task via the unified run_agent path."""
    if task.config_job_name is None:
        return await _run_scheduled_agent(task, deps, occurrence_id=occurrence_id)

    # Temporal BUFFER_ONE serializes normal schedule overlap durably. This
    # process-local lock also serializes duplicate/manual activity delivery so
    # every execution stays in the task's one derived thread.
    lock = _config_job_run_locks.setdefault(task.id, asyncio.Lock())
    async with lock:
        return await _run_scheduled_agent(task, deps, occurrence_id=occurrence_id)


async def _run_scheduled_agent(  # noqa: PLR0911, RUF100 - explicit scheduler terminal outcomes.
    task: ScheduledTask,
    deps: SchedulerDependencies,
    *,
    occurrence_id: str | None,
) -> bool:
    """Run one task after applying config-job serialization."""
    start_time = datetime.now(UTC)
    runtime_folder = task.bound_group_folder
    runtime_jid = task.bound_chat_jid
    if runtime_folder is None or runtime_jid is None:
        await _log_task_error(
            task.id,
            start_time=start_time,
            error="Scheduled task has no durable conversation binding",
        )
        return False
    group = resolve_scheduled_group(deps.workspaces, runtime_folder)
    if group is None or group.jid != runtime_jid:
        await _log_task_error(
            task.id,
            start_time=start_time,
            error=f"Scheduled task runtime is not registered: {runtime_folder}",
        )
        return False
    resolved_occurrence = occurrence_id or f"direct:{task.id}:{start_time.isoformat()}"
    task = await apply_scheduled_session_policy(
        task,
        group,
        resolved_occurrence,
        deps.reset_scheduled_context,
        update_task,
    )

    interrupted_turn = await get_in_flight_turn_for_task(task.id)
    if interrupted_turn is not None:
        if not await claim_in_flight_turn(interrupted_turn.turn_id):
            logger.info(
                "Interrupted scheduled turn already claimed",
                task_id=task.id,
                turn_id=interrupted_turn.turn_id,
            )
            return True
        return await resume_interrupted_scheduled_turn(task, deps, interrupted_turn)
    s = get_settings()
    group_dir = s.groups_dir / runtime_folder
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
        return False

    deterministic = await run_deterministic_config_job(task, cast("ConfigJobExecutionDeps", deps))
    if deterministic is not None:
        return await _finish_scheduled_agent_run(
            task,
            start_time=start_time,
            result=deterministic.result,
            error=deterministic.error,
        )

    prepared_task, skipped_result = await prepare_config_job(task)
    if prepared_task is None:
        return await _finish_scheduled_agent_run(
            task,
            start_time=start_time,
            result=skipped_result,
            error=None,
        )

    execution_task = prepared_task

    async def on_started(target_task: ScheduledTask) -> None:
        await _broadcast_task_start(deps, target_task)

    turn_id, result, error = await run_task_agent(
        TaskAgentRequest(
            task=execution_task,
            deps=cast("ScheduledTurnDeps", deps),
            group=group,
            idle_enabled=True,
            idle_timeout=s.idle_timeout,
            on_started=on_started,
        )
    )
    if error == SCHEDULED_TURN_INTERRUPTED:
        logger.info(
            "Scheduled task yielded to an interactive turn",
            task_id=task.id,
            turn_id=turn_id,
        )
        return False
    return await _finish_scheduled_agent_run(
        task,
        start_time=start_time,
        result=result,
        error=error,
        turn_id=turn_id,
    )
