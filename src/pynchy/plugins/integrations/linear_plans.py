"""Atomic persistence for the Linear planning-approval boundary."""

from __future__ import annotations

from typing import Any

from pynchy.plugins.integrations.linear_client import (
    LinearError,
    LinearQueryClient,
    record_issue_state_update_if_supported,
)

PLAN_START = "<!-- pynchy.plan:start -->"
PLAN_END = "<!-- pynchy.plan:end -->"
_PLAN_HEADING = "## Pynchy implementation plan"


def description_with_plan(description: object, plan: str) -> str:
    """Preserve issue context while inserting or replacing its Pynchy plan."""
    normalized_plan = plan.strip()
    if not normalized_plan:
        raise ValueError("plan is required")
    if PLAN_START in normalized_plan or PLAN_END in normalized_plan:
        raise ValueError("plan cannot contain Pynchy plan markers")

    existing = str(description or "").strip()
    section = f"{PLAN_START}\n{_PLAN_HEADING}\n\n{normalized_plan}\n{PLAN_END}"
    start = existing.find(PLAN_START)
    if start < 0:
        return f"{existing}\n\n{section}".strip()
    end = existing.find(PLAN_END, start)
    if end < 0:
        raise ValueError("existing Linear plan section is incomplete")
    suffix = existing[end + len(PLAN_END) :].strip()
    parts = [existing[:start].strip(), section, suffix]
    return "\n\n".join(part for part in parts if part)


async def update_issue_plan(
    client: LinearQueryClient,
    *,
    issue_id: str,
    state_id: str,
    description: str,
) -> dict[str, Any]:
    """Persist a plan and its approval-wait state in one provider mutation."""
    data = await client.query(
        """
        mutation SubmitPynchyWorkItemPlan(
          $issue_id: String!,
          $state_id: String!,
          $description: String!
        ) {
          issueUpdate(id: $issue_id, input: {
            stateId: $state_id,
            description: $description
          }) {
            success
            issue {
              id identifier title description url updatedAt
              state { id name type }
              project { id name }
            }
          }
        }
        """,
        issue_id=issue_id,
        state_id=state_id,
        description=description,
    )
    result = data.get("issueUpdate")
    if not isinstance(result, dict) or not result.get("success"):
        raise LinearError("Linear did not persist the work-item plan")
    issue = result.get("issue")
    if not isinstance(issue, dict):
        raise LinearError("Linear plan response did not include an issue")
    await record_issue_state_update_if_supported(
        client,
        issue,
        issue_id=issue_id,
        state_id=state_id,
    )
    return issue
