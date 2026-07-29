"""Temporal activity for managed Linear work-item recovery."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, cast

from temporalio import activity

from pynchy.host.orchestrator.temporal.heartbeats import activity_heartbeats
from pynchy.host.orchestrator.temporal.runtime_state import (
    _record_tracked_activity_result,
    _require_scheduler_deps,
)
from pynchy.linear_plan_types import LinearPlanReviewAdmission
from pynchy.scheduling.api import safe_workflow_fragment

if TYPE_CHECKING:
    from pynchy.host.orchestrator.scheduler_deps import SchedulerDependencies

_ACTIVITY_ID = "linear-work-item-reconciliation"
_PLAN_REVIEW_ACTIVITY_PREFIX = "linear-plan-review"


def linear_plan_review_workflow_id(admission: LinearPlanReviewAdmission) -> str:
    """Return the idempotent workflow ID for one provider issue revision."""
    digest = hashlib.sha256(f"{admission.issue_id}:{admission.updated_at}".encode()).hexdigest()[
        :16
    ]
    return f"pynchy-linear-plan-review-{safe_workflow_fragment(admission.identifier)}-{digest}"


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


@activity.defn(name="run_linear_plan_review_admission")
async def run_linear_plan_review_admission(payload: dict[str, Any]) -> str:
    """Review and admit one Human Approved issue without blocking discovery."""
    admission = LinearPlanReviewAdmission.from_payload(payload)
    deps = cast("SchedulerDependencies", _require_scheduler_deps())
    activity_id = f"{_PLAN_REVIEW_ACTIVITY_PREFIX}:{admission.identifier}"
    try:
        async with activity_heartbeats(activity_id):
            admitted = await deps.process_linear_plan_review_admission(admission)
    except Exception as exc:
        _record_tracked_activity_result(activity_id, "error", type(exc).__name__)
        raise
    result = "admitted" if admitted else "stale"
    _record_tracked_activity_result(activity_id, result)
    return result
