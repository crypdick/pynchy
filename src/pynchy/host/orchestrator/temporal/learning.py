"""Temporal learning-review activity wiring."""

from __future__ import annotations

from typing import Any, cast

from temporalio import activity

from pynchy.host.orchestrator.scheduler_deps import (
    SchedulerDependencies,
)
from pynchy.host.orchestrator.temporal.runtime_state import (
    _record_activity_result,
    _require_scheduler_deps,
)
from pynchy.host.orchestrator.temporal.schedules import safe_workflow_fragment
from pynchy.learning_packets import (
    LearningPacket,  # beartype resolves Temporal learning annotations at runtime.
    packet_from_payload,
)


def learning_review_workflow_id(packet: LearningPacket) -> str:
    """Return the idempotency key for one hidden learning review."""
    return f"pynchy-learning-review-{safe_workflow_fragment(packet.job_id)}"


@activity.defn(name="run_learning_review")
async def run_learning_review(packet_payload: dict[str, Any]) -> str:
    """Temporal activity that runs one hidden Obsidian learning review."""
    packet = packet_from_payload(packet_payload)
    try:
        result = await cast("SchedulerDependencies", _require_scheduler_deps()).run_learning_review(
            packet
        )
    except Exception as exc:  # allow: exception-handling; record activity failure.
        _record_activity_result(packet.job_id, "error", str(exc))
        raise
    _record_activity_result(packet.job_id, result)
    return result
