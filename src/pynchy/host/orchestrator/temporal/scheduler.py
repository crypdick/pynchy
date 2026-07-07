"""Temporal runtime for scheduled Pynchy work.

Temporal owns durable execution; activities delegate to the existing host
runner so container IPC and streaming behavior stay in one place.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any

from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker, WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from pynchy.config.models import SchedulerConfig
from pynchy.host.orchestrator.task_scheduler import SchedulerDependencies, _run_scheduled_agent
from pynchy.host.orchestrator.temporal.workflows import ScheduledAgentTaskWorkflow
from pynchy.logger import logger
from pynchy.state import get_task_by_id
from pynchy.types import ScheduledTask

_scheduler_deps: SchedulerDependencies | None = None
_TEMPORAL_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_WORKFLOW_MODULE = "pynchy.host.orchestrator.temporal.workflows"


@dataclass(frozen=True)
class TemporalSchedulerStatusSnapshot:
    worker_running: bool = False
    last_workflow_id: str | None = None
    last_task_id: str | None = None
    last_result: str | None = None
    last_started_at: str | None = None
    last_completed_at: str | None = None
    last_error: str | None = None


_temporal_scheduler_status = TemporalSchedulerStatusSnapshot()


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat()


def reset_temporal_scheduler_status() -> None:
    """Clear the in-process Temporal worker status snapshot."""
    global _temporal_scheduler_status
    _temporal_scheduler_status = TemporalSchedulerStatusSnapshot()


def get_temporal_scheduler_status() -> dict[str, Any]:
    """Return the in-process Temporal worker status snapshot."""
    return asdict(_temporal_scheduler_status)


def _update_temporal_scheduler_status(**changes: Any) -> None:
    global _temporal_scheduler_status
    _temporal_scheduler_status = replace(_temporal_scheduler_status, **changes)


def _activity_workflow_id() -> str | None:
    try:
        return activity.info().workflow_id
    except RuntimeError:
        return None


def _record_activity_result(task_id: str, result: str, error: str | None = None) -> None:
    _update_temporal_scheduler_status(
        last_workflow_id=_activity_workflow_id(),
        last_task_id=task_id,
        last_result=result,
        last_completed_at=_utc_timestamp(),
        last_error=error,
    )


def bind_scheduler_deps(deps: SchedulerDependencies | None) -> None:
    """Bind app dependencies for Temporal activities running in this process."""
    global _scheduler_deps
    _scheduler_deps = deps


def _require_scheduler_deps() -> SchedulerDependencies:
    if _scheduler_deps is None:
        raise RuntimeError("Temporal scheduler dependencies are not bound")
    return _scheduler_deps


def _safe_workflow_fragment(value: str) -> str:
    return _TEMPORAL_SAFE.sub("-", value).strip("-").replace("+", "-")


def agent_task_workflow_id(task: ScheduledTask) -> str:
    """Return the idempotency key for a due scheduled agent task."""
    due_at = task.next_run or "unscheduled"
    return f"pynchy-agent-task-{_safe_workflow_fragment(task.id)}-{_safe_workflow_fragment(due_at)}"


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

    try:
        await _run_scheduled_agent(task, _require_scheduler_deps())
    except Exception as exc:  # allow: exception-handling - record activity failure
        _record_activity_result(task_id, "error", str(exc))
        raise
    _record_activity_result(task_id, "completed")
    return "completed"


class TemporalSchedulerRuntime:
    """Owns the Temporal client and worker used by scheduled agent tasks."""

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
                workflows=[ScheduledAgentTaskWorkflow],
                activities=[run_scheduled_agent_task],
                workflow_runner=scheduler_workflow_runner(),
            )
            await self._worker_stack.enter_async_context(self._worker)
        except BaseException as exc:  # allow: exception-handling - startup cleanup then re-raise
            await self._worker_stack.aclose()
            bind_scheduler_deps(None)
            _update_temporal_scheduler_status(worker_running=False, last_error=str(exc))
            raise
        _update_temporal_scheduler_status(worker_running=True, last_error=None)
        logger.info(
            "Temporal scheduler runtime started",
            address=self.scheduler_config.temporal_address,
            namespace=self.scheduler_config.temporal_namespace,
            task_queue=self.scheduler_config.temporal_task_queue,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._worker_stack.aclose()
        bind_scheduler_deps(None)
        _update_temporal_scheduler_status(worker_running=False)

    async def start_scheduled_agent_task(self, task: ScheduledTask) -> None:
        """Start a Temporal workflow for the due task if one is not already running."""
        if self.client is None:
            raise RuntimeError("Temporal scheduler runtime has not been started")

        workflow_id = agent_task_workflow_id(task)
        try:
            await self.client.start_workflow(
                ScheduledAgentTaskWorkflow.run,
                task.id,
                id=workflow_id,
                task_queue=self.scheduler_config.temporal_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            _update_temporal_scheduler_status(
                last_workflow_id=workflow_id,
                last_task_id=task.id,
                last_result="already_started",
                last_started_at=_utc_timestamp(),
                last_completed_at=None,
                last_error=None,
            )
            logger.debug(
                "Temporal scheduled task workflow already started",
                task_id=task.id,
                workflow_id=workflow_id,
            )
            return
        except Exception as exc:  # allow: exception-handling - record dispatch failure
            _update_temporal_scheduler_status(
                last_workflow_id=workflow_id,
                last_task_id=task.id,
                last_result="error",
                last_started_at=_utc_timestamp(),
                last_completed_at=None,
                last_error=str(exc),
            )
            raise

        _update_temporal_scheduler_status(
            last_workflow_id=workflow_id,
            last_task_id=task.id,
            last_result="started",
            last_started_at=_utc_timestamp(),
            last_completed_at=None,
            last_error=None,
        )

        logger.info(
            "Temporal scheduled task workflow started",
            task_id=task.id,
            workflow_id=workflow_id,
            task_queue=self.scheduler_config.temporal_task_queue,
        )
