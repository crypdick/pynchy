"""Public tool-contract tests for host-owned Linear lifecycle actions."""

from __future__ import annotations

import pytest

from agent_runner.agent_tools import list_tools


@pytest.mark.asyncio
async def test_linear_tool_contract_keeps_human_approval_out_of_agent_control() -> None:
    tools = {tool.name: tool for tool in await list_tools()}

    claim = tools["linear_claim_work_item"]
    move = tools["linear_move_todo"]
    await_review = tools["linear_await_review_work_item"]

    assert "Human Approved" in (claim.description or "")
    assert move.inputSchema["properties"]["status"]["enum"] == [
        "agent_proposed",
        "awaiting_plan_approval",
    ]
    assert "pull_request_url" in await_review.inputSchema["required"]
    assert "merged-PR webhook" in (await_review.description or "")
