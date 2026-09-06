"""Linear board mutations that emit durable webhook-effect evidence."""

from __future__ import annotations

from typing import Any

from pynchy.plugins.integrations import linear_client as linear_effects
from pynchy.plugins.integrations.linear_board_payloads import payload_entity
from pynchy.plugins.integrations.linear_board_queries import (
    MOVE_WORKSPACE_TODO_MUTATION,
)
from pynchy.plugins.integrations.linear_client import (
    LinearQueryClient,
)
from pynchy.plugins.integrations.linear_webhook_evidence import (
    issue_state_mutation_intent,
)


async def apply_workspace_todo_move(
    client: LinearQueryClient,
    *,
    issue_id: str,
    state_id: str,
) -> dict[str, Any]:
    """Apply a board move and require the matching provider receipt."""
    async with linear_effects.linear_webhook_effect(
        client,
        "Issue",
        "update",
        issue_id,
        intent_fingerprint=issue_state_mutation_intent(issue_id, state_id),
    ) as effect:
        data = await client.query(
            MOVE_WORKSPACE_TODO_MUTATION,
            issue_id=issue_id,
            state_id=state_id,
        )
        result = data.get("issueUpdate")
        if not isinstance(result, dict) or not result.get("success"):
            await effect.fail()
        issue = payload_entity(data, "issueUpdate", "issue")
        await linear_effects.confirm_issue_state_effect(
            effect,
            issue,
            issue_id=issue_id,
            state_id=state_id,
        )
        return issue
