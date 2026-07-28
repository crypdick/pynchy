"""Temporal activity for managed Linear work-item recovery."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from temporalio import activity

from pynchy.host.orchestrator.temporal.runtime_state import (
    _record_tracked_activity_result,
    _require_scheduler_deps,
)

if TYPE_CHECKING:
    from pynchy.host.orchestrator.scheduler_deps import SchedulerDependencies

_ACTIVITY_ID = "linear-work-item-reconciliation"


@activity.defn(name="run_linear_work_item_reconciliation")
async def run_linear_work_item_reconciliation() -> str:
    """Repair missing or orphaned execution tasks across managed boards."""
    deps = cast("SchedulerDependencies", _require_scheduler_deps())
    try:
        admitted = await deps.reconcile_linear_work_items()
    except Exception as exc:  # record the Temporal activity failure.
        _record_tracked_activity_result(_ACTIVITY_ID, "error", type(exc).__name__)
        raise
    if admitted is None:
        _record_tracked_activity_result(_ACTIVITY_ID, "disabled")
        return "disabled"
    result = f"completed:{admitted}"
    _record_tracked_activity_result(_ACTIVITY_ID, result)
    return result
