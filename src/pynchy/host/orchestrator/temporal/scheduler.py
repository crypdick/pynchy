from __future__ import annotations

# allow: file-length -- Temporal worker runtime and registration must stay co-located.
# Temporal owns durable execution; activities use the existing host runner so
# container IPC and streaming behavior stay in one place.
import asyncio
import contextlib
from collections.abc import (
    Callable,  # noqa: TC003, RUF100 - beartype resolves Temporal scheduler annotations at runtime.
)
from dataclasses import dataclass, replace
from datetime import (
    timedelta,  # noqa: TC003, RUF100 - beartype resolves Temporal scheduler annotations at runtime.
)
from types import (
    TracebackType,  # noqa: TC003, RUF100 - beartype resolves Temporal scheduler annotations at runtime.
)
from typing import Any, cast

from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker, WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from pynchy.canaries import run_declared_canaries
from pynchy.config import get_settings
from pynchy.config.scheduler_models import (
    SchedulerConfig,  # noqa: TC001, RUF100 - beartype resolves Temporal scheduler annotations at runtime.
)
from pynchy.host.learning.packet_codec import packet_to_payload
from pynchy.host.learning.packet_models import (
    LearningPacket,  # noqa: TC001, RUF100 - beartype resolves Temporal scheduler annotations at runtime.
)
from pynchy.host.orchestrator.scheduled_binding import ensure_scheduled_task_binding
from pynchy.host.orchestrator.task_scheduler import (
    SchedulerDependencies,
    run_scheduled_agent,
)
from pynchy.host.orchestrator.temporal.channel_reconciliation import (
    run_channel_reconciliation,
)
from pynchy.host.orchestrator.temporal.deploy import (
    DeployRequest,
    deploy_request_to_payload,
    deploy_workflow_id,
    run_deploy,
)
from pynchy.host.orchestrator.temporal.git_sync import (
    run_external_git_sync,
    run_host_git_sync,
)
from pynchy.host.orchestrator.temporal.heartbeats import activity_heartbeats
from pynchy.host.orchestrator.temporal.host_jobs import (
    run_config_host_cron_job,
    run_database_host_job,
)
from pynchy.host.orchestrator.temporal.interactive import (
    interactive_message_workflow_id,
    run_interactive_message_turn,
)
from pynchy.host.orchestrator.temporal.interrupted import run_interrupted_agent_turn
from pynchy.host.orchestrator.temporal.learning import (
    learning_review_workflow_id,
    run_learning_review,
)
from pynchy.host.orchestrator.temporal.linear_work_items import (
    run_linear_work_item_reconciliation,
)
from pynchy.host.orchestrator.temporal.runtime_state import (
    _activity_workflow_id,
    _record_activity_result,
    _require_scheduler_deps,
    _update_temporal_scheduler_status,
    _utc_timestamp,
    bind_scheduler_deps,
    parse_temporal_activity_info,
)
from pynchy.host.orchestrator.temporal.runtime_state import (
    get_temporal_scheduler_status as _get_temporal_scheduler_status,
)
from pynchy.host.orchestrator.temporal.runtime_state import (
    reset_temporal_scheduler_status as _reset_temporal_scheduler_status,
)
from pynchy.host.orchestrator.temporal.schedule_reconciler import (
    reconcile_temporal_schedules,
)
from pynchy.host.orchestrator.temporal.schedules import (
    agent_task_schedule_id,
    agent_task_workflow_id,
    channel_reconciliation_schedule_id,
    is_stale_agent_task_once_workflow,
    safe_workflow_fragment,
)
from pynchy.host.orchestrator.temporal.workflows import (
    CanaryRunWorkflow,
    ChannelReconciliationWorkflow,
    ConfigHostCronWorkflow,
    DatabaseHostJobWorkflow,
    DeployWorkflow,
    ExternalGitSyncWorkflow,
    HostGitSyncWorkflow,
    InteractiveMessageWorkflow,
    InterruptedTurnWorkflow,
    LearningReviewWorkflow,
    LinearWorkItemReconciliationWorkflow,
    ScheduledAgentTaskWorkflow,
)
from pynchy.logger import logger
from pynchy.state import (
    claim_deployment,
    clear_pending_deployment,
    clear_unclaimed_in_flight_turn_for_task,
    get_all_host_jobs,
    get_all_tasks,
    get_task_by_id,
)
from pynchy.types import (
    CanaryRun,  # noqa: TC001, RUF100 - beartype resolves canary notification annotations.
    DeployClaim,
    DeployClaimStatus,
    ScheduledTask,  # noqa: TC001, RUF100 - beartype resolves Temporal scheduler annotations at runtime.
)


@dataclass
class _ActiveRuntimeState:
    active_runtime: TemporalSchedulerRuntime | None = None


_state = _ActiveRuntimeState()
_WORKFLOW_MODULE = "pynchy.host.orchestrator.temporal.workflows"
_TEMPORAL_SCHEDULER_NOT_STARTED_ERROR = "Temporal scheduler runtime has not been started"
__all__ = [
    "TemporalRuntimeUnavailableError",
    "TemporalSchedulerRuntime",
    "agent_task_schedule_id",
    "agent_task_workflow_id",
    "cancel_scheduled_agent_workflow",
    "deploy_workflow_id",
    "get_temporal_scheduler_status",
    "interactive_message_workflow_id",
    "interrupted_turn_workflow_id",
    "learning_review_workflow_id",
    "reset_temporal_scheduler_status",
    "scheduler_workflow_runner",
    "start_channel_reconciliation_workflow",
    "start_deploy_workflow",
    "start_interactive_message_workflow",
    "start_interrupted_turn_workflow",
    "start_learning_review_workflow",
    "start_scheduled_agent_task_workflow",
    "temporal_scheduler_runtime_active",
]


class TemporalRuntimeUnavailableError(RuntimeError):
    """Raised when a workflow start is requested before the worker is active."""


def temporal_scheduler_runtime_active() -> bool:
    """Return whether this process currently has an active Temporal runtime."""
    return _state.active_runtime is not None


def interrupted_turn_workflow_id(turn_id: str) -> str:
    """Return the stable Temporal workflow ID for one interrupted agent turn."""
    return f"pynchy-interrupted-turn-{safe_workflow_fragment(turn_id)}"


def reset_temporal_scheduler_status() -> None:
    """Clear the in-process Temporal worker status snapshot."""
    _reset_temporal_scheduler_status()


def get_temporal_scheduler_status() -> dict[str, Any]:
    """Return the in-process Temporal worker status snapshot."""
    return _get_temporal_scheduler_status()


async def _require_active_runtime() -> TemporalSchedulerRuntime:
    """Return the active runtime, waiting briefly for startup to finish."""
    deadline = asyncio.get_running_loop().time() + 10.0
    while _state.active_runtime is None:
        if asyncio.get_running_loop().time() >= deadline:
            raise TemporalRuntimeUnavailableError(_TEMPORAL_SCHEDULER_NOT_STARTED_ERROR)
        await asyncio.sleep(0.05)
    return _state.active_runtime


async def start_learning_review_workflow(packet: LearningPacket) -> None:
    """Start a Temporal learning review workflow using the active runtime."""
    runtime = await _require_active_runtime()
    await runtime.start_learning_review(packet)


async def start_scheduled_agent_task_workflow(task: ScheduledTask) -> None:
    """Start one already-persisted agent task through the active runtime."""
    runtime = await _require_active_runtime()
    await runtime.start_scheduled_agent_task(task)


async def cancel_scheduled_agent_workflow(workflow_id: str) -> bool:
    """Cancel one active scheduled attempt by its durable Temporal identity."""
    runtime = await _require_active_runtime()
    if runtime.client is None:
        raise TemporalRuntimeUnavailableError(_TEMPORAL_SCHEDULER_NOT_STARTED_ERROR)
    try:
        await runtime.client.get_workflow_handle(workflow_id).cancel()
    except RPCError as exc:
        if exc.status is RPCStatusCode.NOT_FOUND:
            return False
        raise
    return True


async def start_interactive_message_workflow(chat_jid: str) -> None:
    """Start a Temporal workflow to process pending messages for one chat."""
    runtime = await _require_active_runtime()
    await runtime.start_interactive_message_turn(chat_jid)


async def start_interrupted_turn_workflow(turn_id: str, chat_jid: str) -> None:
    """Start durable semantic recovery for one interrupted agent turn."""
    runtime = await _require_active_runtime()
    await runtime.start_interrupted_turn(turn_id, chat_jid)


async def start_deploy_workflow(request: DeployRequest) -> DeployClaim:
    """Start a Temporal workflow to perform a deploy handoff."""
    runtime = await _require_active_runtime()
    return await runtime.start_deploy(request)


async def start_channel_reconciliation_workflow() -> None:
    """Start a Temporal workflow to reconcile channel history immediately."""
    runtime = await _require_active_runtime()
    await runtime.start_channel_reconciliation()


def scheduler_workflow_runner() -> WorkflowRunner:
    """Return the Temporal sandbox runner for Pynchy scheduler workflows."""
    # Temporal's sandbox re-imports workflow modules. Pynchy's package import
    # installs beartype import hooks, which are host-process instrumentation
    # rather than workflow logic. Pass through only the deterministic workflow
    # definition module so the sandbox does not re-run that package import path.
    restrictions = SandboxRestrictions.default.with_passthrough_modules(_WORKFLOW_MODULE)
    return SandboxedWorkflowRunner(restrictions=restrictions)


@activity.defn(name="run_scheduled_agent_task")
async def run_scheduled_agent_task(task_id: str) -> str:
    """Temporal activity that runs one active scheduled agent task."""
    task = await get_task_by_id(task_id)
    if task is None or task.status != "active":
        logger.info("Temporal scheduled task skipped", task_id=task_id)
        _record_activity_result(task_id, "skipped")
        return "skipped"
    activity_workflow_id = _activity_workflow_id()
    if activity_workflow_id is not None and is_stale_agent_task_once_workflow(
        task, activity_workflow_id
    ):
        # A one-shot row can be rescheduled while a delayed execution becomes
        # runnable. The immutable workflow ID is the version token, so a
        # mismatched execution cannot run the row's current definition.
        logger.info("Stale Temporal scheduled task skipped", task_id=task_id)
        _record_activity_result(task_id, "skipped")
        return "skipped"

    try:
        completed = await _run_bound_scheduled_agent_task(task)
    except Exception as exc:  # noqa: BLE001, RUF100 - allow: exception-handling; record activity failure.
        _record_activity_result(task_id, "error", str(exc))
        raise
    if completed is False:
        err = "Scheduled agent task requested retry"
        _record_activity_result(task_id, "error", err)
        raise RuntimeError(err)
    _record_activity_result(task_id, "completed")
    return "completed"


async def _run_bound_scheduled_agent_task(task: ScheduledTask) -> bool:
    """Bind and serialize one Temporal occurrence in its thread-owned queue."""
    deps = cast("SchedulerDependencies", _require_scheduler_deps())
    task = await ensure_scheduled_task_binding(task, cast("Any", deps))
    if task.bound_chat_jid is None:
        raise RuntimeError("Scheduled task binding disappeared before queue admission")
    temporal = parse_temporal_activity_info(activity.info())

    async def run_bound_task() -> bool:
        return await run_scheduled_agent(
            task,
            deps,
            occurrence_id=temporal.workflow_run_id or temporal.workflow_id,
        )

    async with activity_heartbeats(task.id):
        return await deps.queue.run_serialized_task(
            task.bound_chat_jid,
            task.id,
            run_bound_task,
        )


@activity.defn(name="clear_terminal_scheduled_turn")
async def clear_terminal_scheduled_turn(task_id: str) -> str:
    """Discard a failed schedule occurrence unless recovery already owns it."""
    cleared = await clear_unclaimed_in_flight_turn_for_task(task_id)
    result = "cleared" if cleared else "preserved"
    logger.info("Terminal scheduled turn cleanup", task_id=task_id, result=result)
    return result


@activity.defn(name="run_scheduled_canaries")
async def run_scheduled_canaries() -> str:
    """Run configured external-service canaries without retrying side effects."""
    canary_config = get_settings().canary
    if not canary_config.enabled:
        _record_activity_result("canaries", "disabled")
        return "disabled"
    scheduler_deps = _require_scheduler_deps()
    try:
        async with activity_heartbeats("canaries"):
            results = await run_declared_canaries(
                target_profile=canary_config.target_profile,
                scenario_ids=canary_config.scenario_ids,
                scheduler_deps=scheduler_deps,
            )
        await _notify_canary_transitions(results, cast("SchedulerDependencies", scheduler_deps))
    except Exception as exc:  # noqa: BLE001, RUF100 - persist operational failure without exposing provider details.
        _record_activity_result("canaries", "error", type(exc).__name__)
        raise
    _record_activity_result("canaries", "completed")
    return f"completed:{len(results)}"


async def _notify_canary_transitions(results: list[CanaryRun], deps: SchedulerDependencies) -> None:
    """Send concise regression and recovery notices to every admin workspace."""
    notices = [
        _canary_transition_notice(result)
        for result in results
        if result.starts_regression or result.is_recovery
    ]
    if not notices:
        return
    admin_jids = [workspace.jid for workspace in deps.workspaces.values() if workspace.is_admin]
    for jid in admin_jids:
        for notice in notices:
            await deps.broadcast_host_message(jid, notice)


def _canary_transition_notice(result: CanaryRun) -> str:
    if result.starts_regression:
        return (
            f"Canary regression: {result.scenario_id} on {result.target_profile} "
            f"({result.error_class or 'unknown failure'}). "
            "See /canaries/report for the unresolved regression report."
        )
    return (
        f"Canary recovered: {result.scenario_id} on {result.target_profile}. "
        "See /canaries/report for current evidence."
    )


class TemporalSchedulerRuntime:
    """Owns the Temporal client, worker, and schedule reconciliation."""

    def __init__(self, deps: SchedulerDependencies, scheduler_config: SchedulerConfig) -> None:
        self.deps = deps
        self.scheduler_config = scheduler_config
        self.client: Client | None = None
        self._worker: Worker | None = None
        self._worker_stack = contextlib.AsyncExitStack()

    async def __aenter__(self) -> TemporalSchedulerRuntime:
        bind_scheduler_deps(self.deps)
        try:
            self.client = await Client.connect(
                self.scheduler_config.temporal_address,
                namespace=self.scheduler_config.temporal_namespace,
            )
            self._worker = Worker(
                self.client,
                task_queue=self.scheduler_config.temporal_task_queue,
                workflows=[
                    DeployWorkflow,
                    HostGitSyncWorkflow,
                    ExternalGitSyncWorkflow,
                    ChannelReconciliationWorkflow,
                    CanaryRunWorkflow,
                    InteractiveMessageWorkflow,
                    InterruptedTurnWorkflow,
                    ScheduledAgentTaskWorkflow,
                    DatabaseHostJobWorkflow,
                    ConfigHostCronWorkflow,
                    LearningReviewWorkflow,
                    LinearWorkItemReconciliationWorkflow,
                ],
                activities=[
                    run_deploy,
                    run_host_git_sync,
                    run_external_git_sync,
                    run_channel_reconciliation,
                    run_scheduled_canaries,
                    run_interactive_message_turn,
                    run_interrupted_agent_turn,
                    run_scheduled_agent_task,
                    clear_terminal_scheduled_turn,
                    run_database_host_job,
                    run_config_host_cron_job,
                    run_learning_review,
                    run_linear_work_item_reconciliation,
                ],
                workflow_runner=scheduler_workflow_runner(),
            )
            await self._worker_stack.enter_async_context(self._worker)
        except BaseException as exc:  # noqa: BLE001, RUF100 - allow: exception-handling; startup cleanup then re-raise.
            await self._worker_stack.aclose()
            bind_scheduler_deps(None)
            _update_temporal_scheduler_status(worker_running=False, last_error=str(exc))
            raise
        _state.active_runtime = self
        _update_temporal_scheduler_status(worker_running=True, last_error=None)
        logger.info(
            "Temporal scheduler runtime started",
            address=self.scheduler_config.temporal_address,
            namespace=self.scheduler_config.temporal_namespace,
            task_queue=self.scheduler_config.temporal_task_queue,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        await self._worker_stack.aclose()
        bind_scheduler_deps(None)
        if _state.active_runtime is self:
            _state.active_runtime = None
        _update_temporal_scheduler_status(worker_running=False)

    async def start_scheduled_agent_task(self, task: ScheduledTask) -> None:
        """Start a Temporal workflow for the due task if one is not already running."""
        if self.client is None:
            raise RuntimeError(_TEMPORAL_SCHEDULER_NOT_STARTED_ERROR)

        workflow_id = agent_task_workflow_id(task)
        await self._start_workflow(
            ScheduledAgentTaskWorkflow.run,
            task.id,
            workflow_id=workflow_id,
            status_id=task.id,
        )

    async def start_learning_review(self, packet: LearningPacket) -> None:
        """Start a Temporal workflow for one hidden learning review packet."""
        if self.client is None:
            raise RuntimeError(_TEMPORAL_SCHEDULER_NOT_STARTED_ERROR)

        await self._start_workflow(
            LearningReviewWorkflow.run,
            packet_to_payload(packet),
            get_settings().learning.max_attempts,
            workflow_id=learning_review_workflow_id(packet),
            status_id=packet.job_id,
        )

    async def start_interactive_message_turn(self, chat_jid: str) -> None:
        """Start a Temporal workflow for pending messages in one chat."""
        if self.client is None:
            raise RuntimeError(_TEMPORAL_SCHEDULER_NOT_STARTED_ERROR)

        settings = get_settings()
        await self._start_workflow(
            InteractiveMessageWorkflow.run,
            chat_jid,
            settings.queue.max_retries + 1,
            float(settings.queue.base_retry_seconds),
            workflow_id=interactive_message_workflow_id(chat_jid),
            status_id=chat_jid,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )

    async def start_interrupted_turn(self, turn_id: str, chat_jid: str) -> None:
        """Start idempotent recovery for a durable interrupted-turn checkpoint."""
        if self.client is None:
            raise RuntimeError(_TEMPORAL_SCHEDULER_NOT_STARTED_ERROR)
        settings = get_settings()
        await self._start_workflow(
            InterruptedTurnWorkflow.run,
            turn_id,
            chat_jid,
            settings.queue.max_retries + 1,
            float(settings.queue.base_retry_seconds),
            workflow_id=interrupted_turn_workflow_id(turn_id),
            status_id=turn_id,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )

    async def start_deploy(self, request: DeployRequest) -> DeployClaim:
        """Start a Temporal workflow for a deploy handoff."""
        if self.client is None:
            raise RuntimeError(_TEMPORAL_SCHEDULER_NOT_STARTED_ERROR)

        claim = await claim_deployment(request.revision, force=request.force)
        if claim.status is not DeployClaimStatus.CLAIMED:
            logger.info(
                "Deploy request skipped",
                commit_sha=request.commit_sha,
                config_hash=request.config_hash,
                reason=request.reason,
                status=claim.status.value,
            )
            return claim

        claimed_request = replace(request, change_kind=claim.change_kind)
        try:
            await self._start_workflow(
                DeployWorkflow.run,
                deploy_request_to_payload(claimed_request),
                workflow_id=deploy_workflow_id(request.revision),
                status_id=request.commit_sha or request.previous_sha or request.reason,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            )
        except Exception:
            await clear_pending_deployment(request.revision)
            raise
        return claim

    async def start_channel_reconciliation(self) -> None:
        """Start a Temporal workflow for immediate channel reconciliation."""
        if self.client is None:
            raise RuntimeError(_TEMPORAL_SCHEDULER_NOT_STARTED_ERROR)

        await self._start_workflow(
            ChannelReconciliationWorkflow.run,
            workflow_id=f"{channel_reconciliation_schedule_id()}-manual",
            status_id=channel_reconciliation_schedule_id(),
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )

    async def reconcile_schedules(self) -> None:
        """Reconcile Pynchy's desired scheduled work into Temporal schedules."""
        if self.client is None:
            raise RuntimeError(_TEMPORAL_SCHEDULER_NOT_STARTED_ERROR)
        await reconcile_temporal_schedules(
            self,
            get_settings_fn=get_settings,
            get_tasks=get_all_tasks,
            get_host_jobs=get_all_host_jobs,
        )

    async def start_temporal_workflow(
        self,
        workflow: Callable[..., object],
        *args: object,
        workflow_id: str,
        status_id: str,
        start_delay: timedelta | None = None,
        id_reuse_policy: WorkflowIDReusePolicy = WorkflowIDReusePolicy.REJECT_DUPLICATE,
    ) -> None:
        """Start a Temporal workflow and update scheduler status consistently."""
        await self._start_workflow(
            workflow,
            *args,
            workflow_id=workflow_id,
            status_id=status_id,
            start_delay=start_delay,
            id_reuse_policy=id_reuse_policy,
        )

    async def _start_workflow(
        self,
        workflow: Callable[..., object],
        *args: object,
        workflow_id: str,
        status_id: str,
        start_delay: timedelta | None = None,
        id_reuse_policy: WorkflowIDReusePolicy = WorkflowIDReusePolicy.REJECT_DUPLICATE,
    ) -> None:
        if self.client is None:
            raise RuntimeError(_TEMPORAL_SCHEDULER_NOT_STARTED_ERROR)

        start_kwargs: dict[str, Any] = {
            "args": list(args),
            "id": workflow_id,
            "task_queue": self.scheduler_config.temporal_task_queue,
            "id_reuse_policy": id_reuse_policy,
        }
        if start_delay is not None:
            start_kwargs["start_delay"] = start_delay

        try:
            await self.client.start_workflow(cast("Any", workflow), **start_kwargs)
        except WorkflowAlreadyStartedError:
            _update_temporal_scheduler_status(
                last_workflow_id=workflow_id,
                last_task_id=status_id,
                last_result="already_started",
                last_started_at=_utc_timestamp(),
                last_completed_at=None,
                last_error=None,
            )
            logger.debug(
                "Temporal scheduled workflow already started",
                work_id=status_id,
                workflow_id=workflow_id,
            )
            return
        except Exception as exc:  # noqa: BLE001, RUF100 - allow: exception-handling; record dispatch failure.
            _update_temporal_scheduler_status(
                last_workflow_id=workflow_id,
                last_task_id=status_id,
                last_result="error",
                last_started_at=_utc_timestamp(),
                last_completed_at=None,
                last_error=str(exc),
            )
            raise

        _update_temporal_scheduler_status(
            last_workflow_id=workflow_id,
            last_task_id=status_id,
            last_result="started",
            last_started_at=_utc_timestamp(),
            last_completed_at=None,
            last_error=None,
        )

        logger.info(
            "Temporal scheduled workflow started",
            work_id=status_id,
            workflow_id=workflow_id,
            task_queue=self.scheduler_config.temporal_task_queue,
        )
