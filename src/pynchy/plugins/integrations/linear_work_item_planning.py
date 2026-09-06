"""Plan persistence for host-managed Linear work items."""

from __future__ import annotations

from typing import Any

from pynchy.plugins.integrations.linear_client import (
    LinearClient,
)
from pynchy.plugins.integrations.linear_plans import description_with_plan, update_issue_plan
from pynchy.plugins.integrations.linear_statuses import (
    AWAITING_PLAN_APPROVAL_STATUS,
    READY_FOR_PLANNING_STATUS,
)
from pynchy.plugins.integrations.linear_work_item_provider import state_id, workspace_issue

_PLANNING_STATE_REQUIRED = (
    "Linear work item must be Ready for Planning or Awaiting Plan Approval before planning"
)


async def submit_work_item_plan(
    client: LinearClient,
    workspace: str,
    issue_id: str,
    plan: str,
) -> dict[str, Any]:
    """Persist an initial or revised plan without crossing human approval."""
    issue, board = await workspace_issue(client, workspace, issue_id)
    planning_state_ids = {
        state_id(board.states[READY_FOR_PLANNING_STATUS]),
        state_id(board.states[AWAITING_PLAN_APPROVAL_STATUS]),
    }
    if state_id(issue) not in planning_state_ids:
        raise ValueError(_PLANNING_STATE_REQUIRED)
    description = description_with_plan(issue.get("description"), plan)
    return await update_issue_plan(
        client,
        issue_id=issue_id,
        state_id=state_id(board.states[AWAITING_PLAN_APPROVAL_STATUS]),
        description=description,
    )
