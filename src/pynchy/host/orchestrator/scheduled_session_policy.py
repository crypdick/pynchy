"""Occurrence-level session policy application."""

from __future__ import annotations

from collections.abc import (
    Awaitable,
    Callable,
)
from dataclasses import replace

from pynchy.scheduling.api import (  # beartype resolves policy annotations at runtime.
    ScheduledTask,
    SessionPolicy,
)
from pynchy.workspace.api import (
    WorkspaceProfile,
)


async def apply_scheduled_session_policy(
    task: ScheduledTask,
    group: WorkspaceProfile,
    occurrence_id: str,
    reset_context: Callable[[ScheduledTask, WorkspaceProfile, str], Awaitable[None]],
    persist_update: Callable[[str, dict[str, object]], Awaitable[None]],
) -> ScheduledTask:
    """Apply one reset at the queue boundary for each scheduled occurrence."""
    if (
        task.session_policy is not SessionPolicy.RESET_BEFORE_RUN
        or task.last_reset_occurrence == occurrence_id
    ):
        return task
    await reset_context(task, group, occurrence_id)
    await persist_update(task.id, {"last_reset_occurrence": occurrence_id})
    return replace(task, last_reset_occurrence=occurrence_id)
