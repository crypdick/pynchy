"""Durable planning-task admission for managed Linear work."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import (
    Awaitable,
    Callable,
)
from dataclasses import dataclass
from datetime import UTC, datetime

from pynchy.content_fencing import fence_untrusted_content
from pynchy.plugins.integrations.linear_boards import (
    WorkspaceLike,
)
from pynchy.plugins.integrations.linear_work_item_tasks import (
    DecisionIssue,
    ensure_task_active,
    linear_issue_conversation_id,
    resume_quiet_paused_task,
)
from pynchy.scheduling.api import (
    ScheduledTask,
    SessionPolicy,
)


@dataclass(frozen=True)
class LinearPlanningTaskRuntime:
    """Durable task reads selected during Linear plugin composition."""

    get_all_tasks: Callable[[], Awaitable[list[ScheduledTask]]]


_runtime: LinearPlanningTaskRuntime | None = None


def configure_linear_planning_task_runtime(runtime: LinearPlanningTaskRuntime) -> None:
    """Set the durable task reads used for planning-task recovery."""
    global _runtime  # noqa: PLW0603 - one host process owns this configured runtime.
    _runtime = runtime


def _configured_runtime() -> LinearPlanningTaskRuntime:
    if _runtime is None:
        raise RuntimeError("Linear planning-task runtime has not been configured")
    return _runtime


def _canonical_task_id(issue: DecisionIssue) -> str:
    digest = hashlib.sha256(issue.id.encode()).hexdigest()[:16]
    return f"linear-plan-{issue.identifier.lower()}-{digest}"


async def _recoverable_task_id(
    issue: DecisionIssue,
    workspace: WorkspaceLike,
) -> str:
    identifier = issue.identifier.lower()
    prefixes = (
        f"linear-plan-{identifier}-",
        f"linear-ready-for-planning-{identifier}-",
    )
    issue_id_pattern = re.compile(rf'"issue_id"\s*:\s*"{re.escape(issue.id)}"')
    candidates = [
        task
        for task in await _configured_runtime().get_all_tasks()
        if task.group_folder == workspace.folder
        and task.id.startswith(prefixes)
        and issue_id_pattern.search(task.prompt) is not None
    ]
    if not candidates:
        return _canonical_task_id(issue)
    return max(candidates, key=lambda task: task.created_at).id


async def admit_planning_issue(
    issue: DecisionIssue,
    workspace: WorkspaceLike,
    *,
    observed_at: datetime,
    public_source: bool,
) -> ScheduledTask | None:
    """Create or recover one planning-only task for a managed ready item."""
    occurred_at = observed_at.astimezone(UTC).isoformat()
    context = json.dumps(
        {
            "issue_id": issue.id,
            "identifier": issue.identifier,
            "title": issue.title,
            "url": issue.url,
            "observed_state": "ready_for_planning",
            "observed_updated_at": issue.updated_at,
        },
        sort_keys=True,
    )
    if public_source:
        context = fence_untrusted_content(context, source="linear-decision-inbox")
    task = ScheduledTask(
        id=await _recoverable_task_id(issue, workspace),
        group_folder=workspace.folder,
        chat_jid=workspace.jid,
        prompt=context,
        schedule_type="once",
        schedule_value=occurred_at,
        session_policy=SessionPolicy.CONTINUE,
        next_run=occurred_at,
        created_at=occurred_at,
        input_source=(f"{'external' if public_source else 'trusted'}:linear:ready_for_planning"),
        derived_thread_name=f"[{issue.identifier}] {issue.title}"[:100],
        conversation_id=await linear_issue_conversation_id(issue.id, workspace.folder),
    )
    active_task, admitted = await ensure_task_active(task, observed_at=observed_at)
    if active_task.status == "paused":
        active_task, resumed = await resume_quiet_paused_task(
            active_task,
            observed_at=observed_at,
        )
        admitted = admitted or resumed
    return active_task if admitted else None
