"""Durable planning-task admission for managed Linear work."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime

from pynchy.host.container_manager.security.fencing import fence_untrusted_content
from pynchy.plugins.integrations.linear_boards import (  # noqa: TC001, RUF100 - beartype resolves task annotations.
    WorkspaceLike,
)
from pynchy.plugins.integrations.linear_work_item_tasks import (
    DecisionIssue,
    ensure_task_active,
)
from pynchy.state import get_all_tasks
from pynchy.types import ScheduledTask

_PLANNING_CONTRACT = (
    "Objective: produce a concrete implementation plan for the exact Ready for Planning "
    "Linear item below.\n"
    "Authority: Ready for Planning authorizes planning only. It does not authorize execution.\n"
    "Success: inspect the repository and relevant documentation, then call linear_submit_plan "
    "with the concrete Markdown plan. That action persists the plan and moves the issue to "
    "Awaiting Plan Approval. Do not execute, claim, or move the item to Human Approved."
)


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
        for task in await get_all_tasks()
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
        prompt=f"{_PLANNING_CONTRACT}\n\n{context}",
        schedule_type="once",
        schedule_value=occurred_at,
        context_mode="isolated",
        next_run=occurred_at,
        created_at=occurred_at,
        input_source=(f"{'external' if public_source else 'trusted'}:linear:ready_for_planning"),
        derived_thread_name=f"[{issue.identifier}] {issue.title}"[:100],
    )
    active_task, admitted = await ensure_task_active(task, observed_at=observed_at)
    return active_task if admitted else None
