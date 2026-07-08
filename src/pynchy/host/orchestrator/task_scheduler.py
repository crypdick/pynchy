"""Schedule reconciler for Temporal-owned Pynchy orchestration."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pynchy.host.container_manager import OnOutput

from pynchy.config import get_settings
from pynchy.host.orchestrator.concurrency import GroupQueue
from pynchy.host.orchestrator.workspace_config import load_workspace_config
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
_scheduler_running = False
TemporalSchedulerRuntime: Any | None = None
_STAGNATION_THRESHOLD = 3
_NO_PROGRESS_THRESHOLD = 5


@runtime_checkable
class TemporalRuntime(Protocol):
    async def reconcile_schedules(self) -> None: ...


def _build_temporal_runtime(deps: SchedulerDependencies, scheduler_config: Any) -> Any:
    """Build the Temporal runtime lazily to avoid a scheduler module import cycle."""
    global TemporalSchedulerRuntime
    if TemporalSchedulerRuntime is None:
        from pynchy.host.orchestrator.temporal.scheduler import (
            TemporalSchedulerRuntime as _TemporalSchedulerRuntime,
        )

        TemporalSchedulerRuntime = _TemporalSchedulerRuntime
    return TemporalSchedulerRuntime(deps, scheduler_config)


def _workspace_map(deps: SchedulerDependencies) -> dict[str, WorkspaceProfile]:
    workspaces = deps.workspaces
    return workspaces() if callable(workspaces) else workspaces


async def start_scheduler_loop(deps: SchedulerDependencies) -> None:
    """Start the scheduler polling loop."""
    global _scheduler_running
    async with _scheduler_lock:
        if _scheduler_running:
            logger.debug("Scheduler loop already running, skipping duplicate start")
            return
        _scheduler_running = True
    scheduler_config = get_settings().scheduler
    logger.info("Scheduler loop started", backend="temporal")

    try:
        async with _build_temporal_runtime(deps, scheduler_config) as temporal_runtime:
            await _run_scheduler_loop(deps, temporal_runtime)
    finally:
        async with _scheduler_lock:
            _scheduler_running = False


async def _run_scheduler_loop(
    deps: SchedulerDependencies, temporal_runtime: TemporalRuntime
) -> None:
    """Reconcile desired scheduled work into Temporal-owned schedules."""
    while True:
        try:
            await temporal_runtime.reconcile_schedules()
        except Exception:
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


async def _scheduled_task_circuit_breaker(task_id: str) -> tuple[str, str] | None:
    logs = await get_task_run_logs(task_id, limit=_NO_PROGRESS_THRESHOLD)
    failure_run: list[TaskRunLog] = []
    for log in logs:
        if log.status != "error":
            break
        failure_run.append(log)

    if len(failure_run) >= _STAGNATION_THRESHOLD:
        last_signature = failure_run[0].error_signature or error_signature(
            failure_run[0].error or ""
        )
        same = 0
        for log in failure_run:
            signature = log.error_signature or error_signature(log.error or "")
            if signature != last_signature:
                break
            same += 1
        if same >= _STAGNATION_THRESHOLD:
            return (
                "stagnation",
                f'Same error repeated {same} times in a row: "{last_signature}".',
            )

    if len(failure_run) >= _NO_PROGRESS_THRESHOLD:
        return (
            "no-progress",
            f"{len(failure_run)} consecutive scheduled-task failures with no success.",
        )

    return None


async def _run_scheduled_agent(task: ScheduledTask, deps: SchedulerDependencies) -> bool:
    """Execute a single scheduled agent task via the unified run_agent path."""
    start_time = datetime.now(UTC)
    s = get_settings()
    group_dir = s.groups_dir / task.group_folder
    group_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Running scheduled task", task_id=task.id, group=task.group_folder)

    groups = _workspace_map(deps)
    group = next((g for g in groups.values() if g.folder == task.group_folder), None)

    if not group:
        logger.error(
            "Group not found for task",
            task_id=task.id,
            group_folder=task.group_folder,
        )
        await log_task_run(
            TaskRunLog(
                task_id=task.id,
                run_at=datetime.now(UTC).isoformat(),
                duration_ms=(datetime.now(UTC) - start_time).total_seconds() * 1000,
                status="error",
                result=None,
                error=f"Group not found: {task.group_folder}",
            )
        )
        return False

    circuit_decision = await _scheduled_task_circuit_breaker(task.id)
    if circuit_decision is not None:
        trigger, reason = circuit_decision
        workflow_id, attempt = _temporal_attempt_metadata()
        await update_task(task.id, {"status": "paused"})
        await log_task_run(
            TaskRunLog(
                task_id=task.id,
                run_at=datetime.now(UTC).isoformat(),
                duration_ms=(datetime.now(UTC) - start_time).total_seconds() * 1000,
                status="error",
                result=None,
                error=reason,
                temporal_workflow_id=workflow_id,
                temporal_attempt=attempt,
                escalation_reason=trigger,
            )
        )
        logger.warning("Scheduled task paused by circuit breaker", task_id=task.id, reason=reason)
        return False

    # Advance next_run BEFORE execution so subsequent Temporal reconciliation
    # does not start another one-shot workflow while this task is still running.
    # The definitive next_run is recalculated AFTER execution based on actual
    # completion time, which matters for long-running tasks.
    next_run = compute_next_run(task.schedule_type, task.schedule_value, s.timezone)
    await update_task(task.id, {"next_run": next_run})

    from pynchy.types import OutboundEvent, OutboundEventType

    await deps.broadcast_to_channels(
        task.chat_jid,
        OutboundEvent(type=OutboundEventType.SYSTEM, content="\u23f1 Scheduled task starting."),
    )

    result: str | None = None
    error: str | None = None

    # Idle timer: close container stdin after IDLE_TIMEOUT of no output,
    # so the container exits instead of hanging at waitForIpcMessage.
    ws_config = load_workspace_config(task.group_folder)
    idle_enabled = (
        ws_config.idle_terminate if ws_config and ws_config.idle_terminate is not None else True
    )

    def _idle_timeout_callback() -> None:
        logger.debug("Scheduled task idle timeout, closing stdin", task_id=task.id)
        deps.queue.close_stdin(task.chat_jid)

    idle_timer = IdleTimer(s.idle_timeout, _idle_timeout_callback) if idle_enabled else None

    try:
        # Convert task prompt to SDK message format
        task_messages = [
            {
                "message_type": "user",
                "sender": "scheduled_task",
                "sender_name": "Scheduled Task",
                "content": task.prompt,
                "timestamp": datetime.now(UTC).isoformat(),
                "metadata": {"source": "scheduled_task"},
            }
        ]

        async def _on_output(streamed: ContainerOutput) -> None:
            nonlocal result, error
            # Delegate to the full output handler (thinking, tool_use,
            # tool_result, system, metadata, result — all broadcast).
            await deps.handle_streamed_output(task.chat_jid, group, streamed)

            # Reset idle timer on every output event so the timeout only
            # fires after a period of complete silence.  Previously this
            # only reset on streamed.result (the final event), which meant
            # the timer never caught agents that hung mid-task.
            if idle_timer:
                idle_timer.reset()
            if streamed.result:
                result = streamed.result
            if streamed.status == "error":
                error = streamed.error or "Unknown error"

        # Start the idle timer before launching the agent so that a
        # container that never produces any output still gets terminated.
        # The session's own idle timer is disabled for scheduled tasks
        # (idle_timeout_override=0.0), so this is the only idle protection.
        if idle_timer:
            idle_timer.reset()

        agent_result = await deps.run_agent(
            group,
            task.chat_jid,
            task_messages,
            _on_output,
            is_scheduled_task=True,
            repo_access_override=task.repo_access,
            input_source="scheduled_task",
        )

        if agent_result == "error":
            error = error or "Agent returned error"

        elapsed_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000
        logger.info("Task completed", task_id=task.id, duration_ms=elapsed_ms)

        # Merge worktree commits respecting the workspace's git_policy
        if not error and task.repo_access:
            from pynchy.host.git_ops._worktree_merge import merge_worktree_with_policy

            await merge_worktree_with_policy(task.group_folder)
    except Exception as exc:
        error = str(exc)
        logger.error("Task failed", task_id=task.id, error=error)
    finally:
        if idle_timer:
            idle_timer.cancel()

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
    next_run = compute_next_run(task.schedule_type, task.schedule_value, s.timezone)

    result_summary = f"Error: {error}" if error else (result[:200] if result else "Completed")
    await update_task_after_run(task.id, next_run, result_summary)
    return error is None
