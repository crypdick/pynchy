"""Curated Temporal scheduler capabilities."""

from typing import cast

from pynchy.host.orchestrator.temporal.deploy import DeployRequest
from pynchy.host.orchestrator.temporal.runtime_state import (
    TemporalActivityInfo,
    parse_temporal_activity_info,
)
from pynchy.host.orchestrator.temporal.schedules import agent_task_workflow_id
from pynchy.host.orchestrator.temporal.status import get_temporal_orchestration_states
from pynchy.host.orchestrator.temporal.workflow_control import (
    TemporalRuntimeUnavailableError,
    cancel_scheduled_agent_workflow,
)
from pynchy.learning_packets import LearningPacket
from pynchy.types import DeployClaim


def get_temporal_scheduler_runtime() -> type[object]:
    """Return the scheduler implementation without creating an import cycle."""
    from pynchy.host.orchestrator.temporal.scheduler import (  # noqa: PLC0415, RUF100 - scheduler imports the application facade.
        TemporalSchedulerRuntime,
    )

    return TemporalSchedulerRuntime


def get_temporal_scheduler_status() -> dict[str, object]:
    """Return the live Temporal scheduler status."""
    from pynchy.host.orchestrator.temporal.scheduler import (  # noqa: PLC0415, RUF100 - scheduler imports the application facade.
        get_temporal_scheduler_status as get_status,
    )

    return cast("dict[str, object]", get_status())


async def start_deploy_workflow(request: DeployRequest) -> DeployClaim:
    """Start the deploy workflow for one request."""
    from pynchy.host.orchestrator.temporal.scheduler import (  # noqa: PLC0415, RUF100 - scheduler imports the application facade.
        start_deploy_workflow as start,
    )

    return await start(request)


async def start_learning_review_workflow(packet: LearningPacket) -> None:
    """Start the hidden learning-review workflow for one packet."""
    from pynchy.host.orchestrator.temporal.scheduler import (  # noqa: PLC0415, RUF100 - scheduler imports the application facade.
        start_learning_review_workflow as start,
    )

    await start(packet)


__all__ = [
    "DeployRequest",
    "TemporalActivityInfo",
    "TemporalRuntimeUnavailableError",
    "agent_task_workflow_id",
    "cancel_scheduled_agent_workflow",
    "get_temporal_orchestration_states",
    "get_temporal_scheduler_runtime",
    "get_temporal_scheduler_status",
    "parse_temporal_activity_info",
    "start_deploy_workflow",
    "start_learning_review_workflow",
]
