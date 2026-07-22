"""Public tool-contract tests for host-owned Linear lifecycle actions."""

from __future__ import annotations

import pytest

from agent_runner.agent_tools import list_tools


@pytest.mark.asyncio
async def test_linear_tool_contract_keeps_human_approval_out_of_agent_control() -> None:
    tools = {tool.name: tool for tool in await list_tools()}

    claim = tools["linear_claim_work_item"]
    move = tools["linear_move_todo"]
    requested = tools["linear_create_requested_todo"]
    submit_plan = tools["linear_submit_plan"]
    await_review = tools["linear_await_review_work_item"]

    assert "Human Approved" in (claim.description or "")
    assert move.inputSchema["properties"]["status"]["enum"] == ["agent_proposed"]
    assert requested.inputSchema["required"] == ["title", "authorization_quote"]
    assert "current direct human message" in (requested.description or "")
    assert "not execution" in (requested.description or "")
    assert submit_plan.inputSchema["required"] == ["issue_id", "plan"]
    assert "does not authorize" in (submit_plan.description or "")
    assert "pull_request_url" in await_review.inputSchema["properties"]
    assert "pull_request_url" not in await_review.inputSchema["required"]
    assert await_review.inputSchema["required"] == ["issue_id", "summary"]
    assert "human acceptance" in (await_review.description or "")
    assert "status" not in await_review.inputSchema["properties"]
