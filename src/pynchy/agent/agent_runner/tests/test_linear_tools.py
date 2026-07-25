"""Public tool-contract tests for agent-managed Linear work."""

from __future__ import annotations

import pytest

from agent_runner.agent_tools import list_tools


@pytest.mark.asyncio
async def test_linear_tools_expose_generic_actions_without_workflow_ritual() -> None:
    tools = {tool.name: tool for tool in await list_tools()}

    move = tools["linear_move_todo"]

    assert move.inputSchema["properties"]["status"]["enum"] == [
        "agent_proposed",
        "human_approved",
        "awaiting_review",
        "follow_ups",
        "blocked",
        "done",
        "rejected",
    ]
    assert set(move.inputSchema["required"]) == {"issue_id", "status"}
    assert "Follow-ups" in (move.description or "")

    removed_ritual = {
        "linear_create_authorized_work_item",
        "linear_record_work_item_result",
        "linear_submit_plan",
        "linear_claim_work_item",
        "linear_await_review_work_item",
        "linear_block_work_item",
        "linear_handoff_work_item",
    }
    assert removed_ritual.isdisjoint(tools)
