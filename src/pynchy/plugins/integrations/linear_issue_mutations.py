"""Linear issue mutation helpers with durable webhook-effect evidence."""

from __future__ import annotations

from typing import Any

from pynchy.plugins.integrations.linear_client import (
    LinearError,
    LinearQueryClient,
    confirm_issue_state_effect,
    linear_webhook_effect,
)
from pynchy.plugins.integrations.linear_webhook_evidence import (
    issue_state_mutation_intent,
)


async def update_issue_state(
    client: LinearQueryClient,
    issue_id: str,
    state_id: str,
) -> dict[str, Any]:
    """Apply one GraphQL issue-state update and require a provider receipt."""
    async with linear_webhook_effect(
        client,
        "Issue",
        "update",
        issue_id,
        intent_fingerprint=issue_state_mutation_intent(issue_id, state_id),
    ) as effect:
        data = await client.query(
            """
            mutation TransitionPynchyWorkItem($issue_id: String!, $state_id: String!) {
              issueUpdate(id: $issue_id, input: { stateId: $state_id }) {
                success
                issue {
                  id identifier title url updatedAt
                  state { id name type }
                  project { id name }
                }
              }
            }
            """,
            issue_id=issue_id,
            state_id=state_id,
        )
        result = data.get("issueUpdate")
        if not isinstance(result, dict) or not result.get("success"):
            await effect.fail()
            raise LinearError("Linear did not update the work item")
        issue = result.get("issue")
        if not isinstance(issue, dict):
            raise LinearError("Linear work-item update response did not include an issue")
        await confirm_issue_state_effect(
            effect,
            issue,
            issue_id=issue_id,
            state_id=state_id,
        )
        return issue


async def archive_issue(client: LinearQueryClient, issue_id: str) -> dict[str, Any]:
    """Archive an issue without trashing it and require its exact provider receipt."""
    data = await client.query(
        """
        mutation ArchiveIssue($issue_id: String!) {
          issueArchive(id: $issue_id, trash: false) {
            success
            entity { id identifier title archivedAt }
          }
        }
        """,
        issue_id=issue_id,
    )
    result = data.get("issueArchive")
    if not isinstance(result, dict) or result.get("success") is not True:
        raise LinearError("Linear did not archive the issue")
    issue = result.get("entity")
    if (
        not isinstance(issue, dict)
        or issue.get("id") != issue_id
        or not isinstance(issue.get("archivedAt"), str)
        or not issue["archivedAt"].strip()
    ):
        raise LinearError("Linear archive response did not confirm the requested issue")
    return issue
