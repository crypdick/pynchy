"""In-process Temporal worker state shared by runtime and activities."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, cast

from temporalio import activity

from pynchy.turn_outcomes import TurnOutcome

_SCHEDULER_DEPS_NOT_BOUND = "Temporal scheduler dependencies are not bound"


@dataclass(frozen=True)
class TemporalSchedulerStatusSnapshot:
    worker_running: bool = False  # noqa: V107
    last_workflow_id: str | None = None  # noqa: V107
    last_task_id: str | None = None  # noqa: V107
    last_result: str | None = None
    last_started_at: str | None = None  # noqa: V107
    last_completed_at: str | None = None  # noqa: V107
    last_error: str | None = None  # noqa: V107
    tracked_results: dict[str, TemporalTrackedActivitySnapshot] = field(default_factory=dict)


@dataclass(frozen=True)
class TemporalActivityInfo:
    """Pynchy-owned subset of Temporal activity execution metadata."""

    workflow_id: str | None
    workflow_run_id: str | None = None
    attempt: int | None = None


@dataclass(frozen=True)
class TemporalTrackedActivitySnapshot:
    """Latest status retained for an explicitly tracked recurring activity."""

    workflow_id: str | None
    workflow_run_id: str | None
    result: str
    completed_at: str
    error: str | None


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
    _state.temporal_scheduler_status = replace(
        _state.temporal_scheduler_status,
        **cast("Any", changes),
    )


def parse_temporal_activity_info(raw_info: object) -> TemporalActivityInfo:
    """Parse Temporal's rich activity metadata at the scheduler boundary."""
    if isinstance(raw_info, TemporalActivityInfo):
        return raw_info
    raw_workflow_id = getattr(raw_info, "workflow_id", "")
    raw_workflow_run_id = getattr(raw_info, "workflow_run_id", None)
    raw_attempt = getattr(raw_info, "attempt", None)
    return TemporalActivityInfo(
        workflow_id=raw_workflow_id if isinstance(raw_workflow_id, str) else None,
        workflow_run_id=(raw_workflow_run_id if isinstance(raw_workflow_run_id, str) else None),
        attempt=raw_attempt
        if isinstance(raw_attempt, int) and not isinstance(raw_attempt, bool)
        else None,
    )


def _activity_workflow_id() -> str | None:
    try:
        return parse_temporal_activity_info(activity.info()).workflow_id
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


def settle_turn_activity(
    task_id: str,
    outcome: TurnOutcome,
    *,
    retry_error: str,
) -> str:
    """Record one typed turn outcome and translate retry into activity failure."""
    _record_activity_result(task_id, outcome.value)
    if outcome is TurnOutcome.RETRY:
        raise RuntimeError(retry_error)
    return outcome.value


def _record_tracked_activity_result(task_id: str, result: str, error: str | None = None) -> None:
    """Record an activity globally and retain its latest per-task health."""
    try:
        info = parse_temporal_activity_info(activity.info())
    except RuntimeError:
        info = TemporalActivityInfo(workflow_id=None)
    completed_at = _utc_timestamp()
    tracked_results = {
        **_state.temporal_scheduler_status.tracked_results,
        task_id: TemporalTrackedActivitySnapshot(
            workflow_id=info.workflow_id,
            workflow_run_id=info.workflow_run_id,
            result=result,
            completed_at=completed_at,
            error=error,
        ),
    }
    _update_temporal_scheduler_status(
        last_workflow_id=info.workflow_id,
        last_task_id=task_id,
        last_result=result,
        last_completed_at=completed_at,
        last_error=error,
        tracked_results=tracked_results,
    )


def bind_scheduler_deps(deps: object | None) -> None:
    """Bind app dependencies for Temporal activities running in this process."""
    _state.scheduler_deps = deps


def _require_scheduler_deps() -> object:
    if _state.scheduler_deps is None:
        raise RuntimeError(_SCHEDULER_DEPS_NOT_BOUND)
    return _state.scheduler_deps
