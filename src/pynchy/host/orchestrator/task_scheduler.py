"""Schedule reconciler for Temporal-owned Pynchy orchestration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pynchy.host.container_manager import OnOutput
    from pynchy.host.orchestrator.concurrency import GroupQueue

from pynchy.config import get_settings
from pynchy.logger import logger
from pynchy.state import (
    get_task_run_logs,
    log_task_run,
    update_task,
    update_task_after_run,
)
from pynchy.types import ContainerOutput, OutboundEvent, ScheduledTask, TaskRunLog, WorkspaceProfile
from pynchy.utils import IdleTimer, compute_next_run


@runtime_checkable
class SchedulerDependencies(Protocol):
    """Dependencies for the task scheduler."""

    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    @property
    def queue(self) -> GroupQueue: ...

    async def broadcast_to_channels(self, jid: str, event: OutboundEvent) -> None: ...

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...

    async def broadcast_system_notice(self, chat_jid: str, text: str) -> None: ...

    async def run_agent(
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
    ) -> str: ...

    async def handle_streamed_output(
        self, chat_jid: str, group: WorkspaceProfile, result: ContainerOutput
    ) -> bool: ...


_scheduler_lock = asyncio.Lock()
TemporalSchedulerRuntime: Any | None = None
_STAGNATION_THRESHOLD = 3
_NO_PROGRESS_THRESHOLD = 5


@dataclass
class _SchedulerState:
    scheduler_running: bool = False


_state = _SchedulerState()


@runtime_checkable
class _IdleQueue(Protocol):
    def close_stdin(self, chat_jid: str) -> None: ...


class _IdleDeps(SchedulerDependencies, Protocol):
    @property
    def queue(self) -> _IdleQueue: ...


def _recent_failure_run(logs: list[TaskRunLog]) -> list[TaskRunLog]:
    failure_run: list[TaskRunLog] = []
    for log in logs:
        if log.status != "error":
            break
        failure_run.append(log)
    return failure_run


def _stagnation_circuit_decision(failure_run: list[TaskRunLog]) -> tuple[str, str] | None:
    if len(failure_run) < _STAGNATION_THRESHOLD:
        return None

    last_signature = failure_run[0].error_signature or error_signature(failure_run[0].error or "")
    same = 0
    for log in failure_run:
        signature = log.error_signature or error_signature(log.error or "")
        if signature != last_signature:
            break
        same += 1
    if same < _STAGNATION_THRESHOLD:
        return None
    return (
        "stagnation",
        f'Same error repeated {same} times in a row: "{last_signature}".',
    )


def _no_progress_circuit_decision(failure_run: list[TaskRunLog]) -> tuple[str, str] | None:
    if len(failure_run) < _NO_PROGRESS_THRESHOLD:
        return None
    return (
        "no-progress",
        f"{len(failure_run)} consecutive scheduled-task failures with no success.",
    )


@runtime_checkable
class TemporalRuntime(Protocol):
    async def reconcile_schedules(self) -> None: ...


def _build_temporal_runtime(deps: SchedulerDependencies, scheduler_config: Any) -> Any:
    """Build the Temporal runtime lazily to avoid a scheduler module import cycle."""
    runtime_cls = TemporalSchedulerRuntime
    if runtime_cls is None:
        from pynchy.host.orchestrator.temporal.scheduler import (
            TemporalSchedulerRuntime as _TemporalSchedulerRuntime,
        )

        runtime_cls = _TemporalSchedulerRuntime
    return runtime_cls(deps, scheduler_config)


def _workspace_map(deps: SchedulerDependencies) -> dict[str, WorkspaceProfile]:
    workspaces = deps.workspaces
    return workspaces() if callable(workspaces) else workspaces


async def start_scheduler_loop(deps: SchedulerDependencies) -> None:
    """Start the scheduler polling loop."""
    async with _scheduler_lock:
        if _state.scheduler_running:
            logger.debug("Scheduler loop already running, skipping duplicate start")
            return
        _state.scheduler_running = True
    scheduler_config = get_settings().scheduler
    logger.info("Scheduler loop started", backend="temporal")

    try:
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


def resolve_cron_job_cwd(cwd: str | None) -> str:
    """Resolve optional cron job cwd against project root."""
    project_root = get_settings().project_root
    if not cwd:
        return str(project_root)
    path = Path(cwd)
    if path.is_absolute():
        return str(path)
    return str((project_root / path).resolve())


def error_signature(error: str) -> str:
    """Normalize volatile details so repeated failures can be grouped."""
    import re

    first_line = next((line.strip() for line in error.splitlines() if line.strip()), "")
    return re.sub(r"\s+", " ", re.sub(r"\b\d+\b", "#", first_line)).strip()


def _temporal_attempt_metadata() -> tuple[str | None, int | None]:
    try:
        from temporalio import activity

        info = activity.info()
    except RuntimeError:
        return None, None
    return info.workflow_id, info.attempt


def _scheduled_group(
    deps: SchedulerDependencies,
    group_folder: str,
) -> WorkspaceProfile | None:
    groups = _workspace_map(deps)
    return next((group for group in groups.values() if group.folder == group_folder), None)


async def _log_task_error(
    task_id: str,
    *,
    start_time: datetime,
    error: str,
    escalation_reason: str | None = None,
) -> None:
    workflow_id, attempt = _temporal_attempt_metadata()
    await log_task_run(
        TaskRunLog(
            task_id=task_id,
            run_at=datetime.now(UTC).isoformat(),
            duration_ms=(datetime.now(UTC) - start_time).total_seconds() * 1000,
            status="error",
            result=None,
            error=error,
            temporal_workflow_id=workflow_id,
            temporal_attempt=attempt,
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


def _next_run_guard(task: ScheduledTask, timezone: str) -> str | None:
    return compute_next_run(task.schedule_type, task.schedule_value, timezone)


async def _advance_next_run_guard(task: ScheduledTask, timezone: str) -> None:
    await update_task(task.id, {"next_run": _next_run_guard(task, timezone)})


def _scheduled_task_message(task: ScheduledTask) -> dict[str, Any]:
    return {
        "message_type": "user",
        "sender": "scheduled_task",
        "sender_name": "Scheduled Task",
        "content": task.prompt,
        "timestamp": datetime.now(UTC).isoformat(),
        "metadata": {"source": "scheduled_task"},
    }


async def _broadcast_task_start(deps: SchedulerDependencies, task: ScheduledTask) -> None:
    from pynchy.types import OutboundEvent, OutboundEventType

    await deps.broadcast_to_channels(
        task.chat_jid,
        OutboundEvent(type=OutboundEventType.SYSTEM, content="\u23f1 Scheduled task starting."),
    )


def _scheduled_idle_timer(
    deps: _IdleDeps,
    task: ScheduledTask,
    *,
    idle_enabled: bool,
    idle_timeout: float,
) -> IdleTimer | None:
    if not idle_enabled:
        return None

    def _idle_timeout_callback() -> None:
        logger.debug("Scheduled task idle timeout, closing stdin", task_id=task.id)
        deps.queue.close_stdin(task.chat_jid)

    return IdleTimer(idle_timeout, _idle_timeout_callback)


async def _merge_scheduled_task_worktree(task: ScheduledTask, *, error: str | None) -> None:
    if error:
        return

    from pynchy.host.git_ops._worktree_merge import merge_worktree_with_policy

    await merge_worktree_with_policy(task.group_folder)


async def _run_task_agent(
    task: ScheduledTask,
    deps: _IdleDeps,
    group: WorkspaceProfile,
    *,
    idle_enabled: bool,
    idle_timeout: float,
) -> tuple[str | None, str | None]:
    result: str | None = None
    error: str | None = None
    idle_timer = _scheduled_idle_timer(
        deps,
        task,
        idle_enabled=idle_enabled,
        idle_timeout=idle_timeout,
    )

    async def _on_output(streamed: ContainerOutput) -> None:
        nonlocal result, error
        await deps.handle_streamed_output(task.chat_jid, group, streamed)
        if idle_timer:
            idle_timer.reset()
        if streamed.result:
            result = streamed.result
        if streamed.status == "error":
            error = streamed.error or "Unknown error"

    try:
        if idle_timer:
            idle_timer.reset()

        agent_result = await deps.run_agent(
            group,
            task.chat_jid,
            [_scheduled_task_message(task)],
            _on_output,
            is_scheduled_task=True,
            repo_access_override=None,
            input_source="scheduled_task",
        )
        if agent_result == "error":
            error = error or "Agent returned error"
        await _merge_scheduled_task_worktree(task, error=error)
    except Exception as exc:  # noqa: BLE001, RUF100 - task execution is a boundary; record the failure and continue.
        error = str(exc)
        logger.error("Task failed", task_id=task.id, error=error)
        return result, error
    else:
        return result, error
    finally:
        if idle_timer:
            idle_timer.cancel()


async def _scheduled_task_circuit_breaker(task_id: str) -> tuple[str, str] | None:
    logs = await get_task_run_logs(task_id, limit=_NO_PROGRESS_THRESHOLD)
    failure_run = _recent_failure_run(logs)
    return _stagnation_circuit_decision(failure_run) or _no_progress_circuit_decision(failure_run)


async def _run_scheduled_agent(task: ScheduledTask, deps: SchedulerDependencies) -> bool:
    """Execute a single scheduled agent task via the unified run_agent path."""
    start_time = datetime.now(UTC)
    s = get_settings()
    group_dir = s.groups_dir / task.group_folder
    group_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Running scheduled task", task_id=task.id, group=task.group_folder)

    group = _scheduled_group(deps, task.group_folder)
    if not group:
        logger.error(
            "Group not found for task",
            task_id=task.id,
            group_folder=task.group_folder,
        )
        await _log_missing_group(task, start_time)
        return False

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

    # Advance next_run BEFORE execution so subsequent Temporal reconciliation
    # does not start another one-shot workflow while this task is still running.
    # The definitive next_run is recalculated AFTER execution based on actual
    # completion time, which matters for long-running tasks.
    await _advance_next_run_guard(task, s.timezone)

    await _broadcast_task_start(deps, task)
    result, error = await _run_task_agent(
        task,
        deps,
        group,
        idle_enabled=True,
        idle_timeout=s.idle_timeout,
    )
    logger.info(
        "Task completed",
        task_id=task.id,
        duration_ms=(datetime.now(UTC) - start_time).total_seconds() * 1000,
    )

    duration_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000
    workflow_id, attempt = _temporal_attempt_metadata()

    await log_task_run(
        TaskRunLog(
            task_id=task.id,
            run_at=datetime.now(UTC).isoformat(),
            duration_ms=duration_ms,
            status="error" if error else "success",
            result=result,
            error=error,
            temporal_workflow_id=workflow_id,
            temporal_attempt=attempt,
            error_signature=error_signature(error) if error else None,
        )
    )

    # Recalculate next_run from actual completion time.  The pre-execution
    # value (set above) was a guard against re-queuing; this post-execution
    # value is the definitive schedule for the next run.
    next_run = _next_run_guard(task, s.timezone)

    result_summary = f"Error: {error}" if error else (result[:200] if result else "Completed")
    await update_task_after_run(task.id, next_run, result_summary)
    return error is None
