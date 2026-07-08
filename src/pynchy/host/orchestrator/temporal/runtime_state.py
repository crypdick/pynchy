"""In-process Temporal worker state shared by runtime and activities."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any

from temporalio import activity

from pynchy.host.orchestrator.task_scheduler import SchedulerDependencies


@dataclass(frozen=True)
class TemporalSchedulerStatusSnapshot:
    worker_running: bool = False
    last_workflow_id: str | None = None
    last_task_id: str | None = None
    last_result: str | None = None
    last_started_at: str | None = None
    last_completed_at: str | None = None
    last_error: str | None = None


_scheduler_deps: SchedulerDependencies | None = None
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
