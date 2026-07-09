"""In-process Temporal worker state shared by runtime and activities."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime

from temporalio import activity

_SCHEDULER_DEPS_NOT_BOUND = "Temporal scheduler dependencies are not bound"


@dataclass(frozen=True)
class TemporalSchedulerStatusSnapshot:
    worker_running: bool = False
    last_workflow_id: str | None = None
    last_task_id: str | None = None
    last_result: str | None = None
    last_started_at: str | None = None
    last_completed_at: str | None = None
    last_error: str | None = None


@dataclass
class _RuntimeState:
    scheduler_deps: object | None = None
    temporal_scheduler_status: TemporalSchedulerStatusSnapshot = TemporalSchedulerStatusSnapshot()


_state = _RuntimeState()


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat()


def reset_temporal_scheduler_status() -> None:
    """Clear the in-process Temporal worker status snapshot."""
    _state.temporal_scheduler_status = TemporalSchedulerStatusSnapshot()


def get_temporal_scheduler_status() -> dict[str, object]:
    """Return the in-process Temporal worker status snapshot."""
    return asdict(_state.temporal_scheduler_status)


def _update_temporal_scheduler_status(**changes: object) -> None:
    _state.temporal_scheduler_status = replace(_state.temporal_scheduler_status, **changes)


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


def bind_scheduler_deps(deps: object | None) -> None:
    """Bind app dependencies for Temporal activities running in this process."""
    _state.scheduler_deps = deps


def _require_scheduler_deps() -> object:
    if _state.scheduler_deps is None:
        raise RuntimeError(_SCHEDULER_DEPS_NOT_BOUND)
    return _state.scheduler_deps
