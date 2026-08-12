"""Public JSON-RPC behavior for invalid Linear MCP requests."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from pynchy.plugins.integrations.linear import LinearClient, LinearMcpPlugin, build_app, main
from pynchy.workspace.api import WorkspaceProfile


async def _call(
    params: object,
    *,
    method: str = "tools/call",
    workspace: str | None = None,
) -> dict[str, object]:
    client = TestClient(TestServer(build_app(workspace=workspace)))
    await client.start_server()
    try:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        if response.status == 202:
            return {}
        return await response.json()
    finally:
        await client.close()


def _error_text(response: dict[str, object]) -> str:
    result = response["result"]
    assert isinstance(result, dict)
    content = result["content"]
    assert isinstance(content, list)
    item = content[0]
    assert isinstance(item, dict)
    return str(item["text"])


async def test_mcp_handles_initialized_notification_and_unknown_method():
    notification = await _call({}, method="notifications/initialized")
    unknown = await _call({}, method="unknown")

    assert notification == {}
    assert unknown["error"] == {
        "code": -32601,
        "message": "Unknown MCP method: unknown",
    }


async def test_mcp_rejects_non_object_tool_arguments(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")

    response = await _call({"name": "linear_list_teams", "arguments": ["invalid"]})

    assert "Tool arguments must be an object" in _error_text(response)


async def test_mcp_rejects_non_object_tool_params(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")

    response = await _call(["invalid"])

    result = response["result"]
    assert isinstance(result, dict)
    assert result["isError"] is True


async def test_mcp_rejects_invalid_json_rpc_request():
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    try:
        response = await client.post("/mcp", json={"id": 1, "method": "tools/list"})
        payload = await response.json()
    finally:
        await client.close()

    assert payload["error"]["code"] == -32600


async def test_mcp_reports_unknown_tool(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")

    response = await _call({"name": "linear_unknown", "arguments": {}})

    assert "Unknown Linear tool: linear_unknown" in _error_text(response)


async def test_mcp_coerces_issue_list_limit_to_an_integer(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    fake_client = LinearClient(api_key="lin_api_test", session=AsyncMock())
    fake_client.list_issues = AsyncMock(return_value=[])

    with patch("pynchy.plugins.integrations.linear.LinearClient", return_value=fake_client):
        response = await _call({"name": "linear_list_issues", "arguments": {"first": "7"}})

    assert json.loads(_error_text(response)) == []
    fake_client.list_issues.assert_awaited_once_with(team_id=None, first=7)


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        (
            "linear_create_issue",
            {"team_id": "team-1", "title": "Issue", "label_ids": "label-1"},
            "label_ids must be an array",
        ),
        (
            "linear_create_todo",
            {"title": "Todo", "description": 123},
            "description must be a string",
        ),
        (
            "linear_create_attachment",
            {"issue_id": "issue-1", "url": "https://example.com", "title": "Link", "subtitle": 123},
            "subtitle must be a string",
        ),
        (
            "linear_search_issues",
            {"query": "coverage", "team_id": 123},
            "team_id must be a string",
        ),
        (
            "linear_search_issues",
            {"query": "coverage", "unexpected": True},
            "unexpected arguments: unexpected",
        ),
        (
            "linear_search_issues",
            {"query": "coverage", "first": 0},
            "first must be an integer from 1 through 100",
        ),
    ],
)
async def test_mcp_rejects_invalid_tool_arguments(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    arguments: dict[str, object],
    message: str,
):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    workspace = "project" if name == "linear_create_todo" else None

    response = await _call({"name": name, "arguments": arguments}, workspace=workspace)

    assert message in _error_text(response)


@pytest.mark.parametrize(
    ("name", "arguments", "workspace", "message"),
    [
        (
            "linear_list_todos",
            {},
            None,
            "Workspace-scoped Linear todo tools require an MCP workspace instance",
        ),
        ("linear_get_issue", {}, None, "issue_id is required"),
        ("linear_search_issues", {}, None, "query is required"),
    ],
)
async def test_mcp_rejects_missing_tool_context_or_required_values(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    arguments: dict[str, object],
    workspace: str | None,
    message: str,
):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")

    response = await _call(
        {"name": name, "arguments": arguments},
        workspace=workspace,
    )

    assert message in _error_text(response)


def test_main_builds_the_configured_local_server():
    with patch("pynchy.plugins.integrations.linear.web.run_app") as run_app:
        main(["--port", "8490", "--workspace", "project"])

    run_app.assert_called_once()
    assert run_app.call_args.kwargs == {"host": "localhost", "port": 8490}


async def test_unconfigured_plugin_rejects_context_reset_settlement():
    with pytest.raises(RuntimeError, match="not configured"):
        await LinearMcpPlugin().pynchy_before_context_reset(
            WorkspaceProfile(
                jid="discord:channel:project",
                name="Project",
                folder="project",
                trigger="@Pynchy",
            )
        )
