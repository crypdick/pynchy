"""Temporal activity for managed Linear work-item recovery."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from temporalio import activity

from pynchy.host.orchestrator.temporal.runtime_state import (
    _record_tracked_activity_result,
    _require_scheduler_deps,
)
from pynchy.plugins.integrations.linear_boot import linear_workspace_boards
from pynchy.plugins.integrations.linear_decision_inbox import (
    reconcile_all_linear_work_items,
)

if TYPE_CHECKING:
    from pynchy.host.orchestrator.task_scheduler import SchedulerDependencies

_ACTIVITY_ID = "linear-work-item-reconciliation"


@activity.defn(name="run_linear_work_item_reconciliation")
async def run_linear_work_item_reconciliation() -> str:
    """Repair missing or orphaned execution tasks across managed boards."""
    deps = cast("SchedulerDependencies", _require_scheduler_deps())
    boards = linear_workspace_boards()
    if not boards:
        _record_tracked_activity_result(_ACTIVITY_ID, "disabled")
        return "disabled"
    try:
        admitted = await reconcile_all_linear_work_items(
            deps.workspaces,
            boards,
            review_plan=deps.review_linear_plan,
            broadcast_host_message=deps.broadcast_host_message,
        )
    except Exception as exc:  # noqa: BLE001, RUF100 - record the Temporal activity failure.
        _record_tracked_activity_result(_ACTIVITY_ID, "error", type(exc).__name__)
        raise
    result = f"completed:{len(admitted)}"
    _record_tracked_activity_result(_ACTIVITY_ID, result)
    return result
