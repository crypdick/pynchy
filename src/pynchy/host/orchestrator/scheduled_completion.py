"""Domain completion policy for scheduled agent occurrences."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pynchy.host.orchestrator.scheduler_deps import (
    ScheduledCompletionDeps,
)
from pynchy.logger import logger


@dataclass(frozen=True)
class ScheduledAgentOutcome:
    """Application outcome recorded after one agent process exits."""

    status: Literal["success", "incomplete", "error"]
    summary: str


async def classify_scheduled_agent_outcome(
    deps: ScheduledCompletionDeps,
    task_id: str,
    *,
    result: str | None,
    error: str | None,
) -> ScheduledAgentOutcome:
    """Require a bound Linear execution to record an explicit lifecycle outcome."""
    if error is not None:
        return ScheduledAgentOutcome(status="error", summary=f"Error: {error}")

    execution = await deps.scheduled_execution_lifecycle(task_id)
    if execution is None or execution.has_explicit_outcome:
        return ScheduledAgentOutcome(
            status="success",
            summary=result[:200] if result else "Completed",
        )

    logger.warning(
        "Linear task exited without an explicit lifecycle outcome",
        task_id=task_id,
        execution_id=execution.execution_id,
        execution_status=execution.status,
    )
    return ScheduledAgentOutcome(
        status="incomplete",
        summary=f"Incomplete: {result[:188] if result else 'no Linear lifecycle outcome'}",
    )
