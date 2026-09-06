"""Tests for the built-in Linear MCP integration."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from pynchy.plugins.integrations.linear import LinearClient, build_app
from tests.linear_integration_support import (
    start_mcp_client,
)


class TestLinearMcpServer:
    async def test_mcp_initialize_returns_server_info(self, monkeypatch):
        monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
        client = await start_mcp_client()
        try:
            response = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            )

            assert response.status == 200
            payload = await response.json()
            assert payload["result"]["serverInfo"]["name"] == "pynchy-linear"
        finally:
            await client.close()

    async def test_mcp_lists_tools(self, monkeypatch):
        monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
        client = await start_mcp_client()
        try:
            response = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )

            assert response.status == 200
            payload = await response.json()
            names = {tool["name"] for tool in payload["result"]["tools"]}
            assert names == {
                "linear_list_teams",
                "linear_list_issues",
                "linear_search_issues",
                "linear_get_issue",
                "linear_archive_issue",
                "linear_create_issue",
                "linear_list_todos",
                "linear_create_todo",
                "linear_create_attachment",
                "linear_find_issues_by_attachment_url",
            }
        finally:
            await client.close()

    async def test_agent_tools_cannot_assert_human_approval(self, monkeypatch):
        monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
        client = await start_mcp_client()
        try:
            response = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )

            payload = await response.json()
            tools = {tool["name"]: tool for tool in payload["result"]["tools"]}
            assert "state_id" not in tools["linear_create_issue"]["inputSchema"]["properties"]
            assert tools["linear_create_issue"]["inputSchema"]["properties"]["priority"][
                "enum"
            ] == [0, 1, 2, 3, 4]
            todo_properties = tools["linear_create_todo"]["inputSchema"]["properties"]
            assert "status" not in todo_properties
            assert "exact_description" not in todo_properties
            assert todo_properties["description"] == {"type": "string"}
            assert todo_properties["priority"]["enum"] == [0, 1, 2, 3, 4]
            assert "Agent Proposed" in tools["linear_create_todo"]["description"]
        finally:
            await client.close()

    async def test_workspace_todo_rejects_invalid_priority(self, monkeypatch):
        monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
        client = TestClient(TestServer(build_app(workspace="code-improver")))
        await client.start_server()
        try:
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "linear_create_todo",
                        "arguments": {"title": "Review docs", "priority": 5},
                    },
                },
            )

            payload = await response.json()
        finally:
            await client.close()

        assert payload["result"]["isError"] is True
        text = payload["result"]["content"][0]["text"]
        assert "priority must be an integer from 0 through 4" in text

    async def test_ordinary_issue_rejects_invalid_priority(self, monkeypatch):
        monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
        client = await start_mcp_client()
        try:
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "linear_create_issue",
                        "arguments": {"team_id": "team-1", "title": "Issue", "priority": 5},
                    },
                },
            )

            payload = await response.json()
        finally:
            await client.close()

        assert payload["result"]["isError"] is True
        text = payload["result"]["content"][0]["text"]
        assert "priority must be an integer from 0 through 4" in text

    async def test_mcp_reports_missing_api_key(self, monkeypatch):
        monkeypatch.delenv("LINEAR_API_KEY", raising=False)
        client = await start_mcp_client()
        try:
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "linear_list_teams", "arguments": {}},
                },
            )

            assert response.status == 200
            payload = await response.json()
            text = payload["result"]["content"][0]["text"]
            assert "LINEAR_API_KEY is not configured" in text
        finally:
            await client.close()

    @pytest.mark.action("linear.issue.create")
    async def test_mcp_create_issue_calls_client(self, monkeypatch):
        monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
        fake_client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        fake_client.create_issue = AsyncMock(
            return_value={
                "id": "issue-1",
                "identifier": "PYN-1",
                "title": "Track task",
                "url": "https://linear.app/acme/issue/PYN-1",
            }
        )
        with patch("pynchy.plugins.integrations.linear.LinearClient", return_value=fake_client):
            client = await start_mcp_client()
            try:
                response = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "linear_create_issue",
                            "arguments": {
                                "team_id": "team-1",
                                "title": "Track task",
                                "description": "Task details",
                                "label_ids": ["label-1"],
                                "priority": 4,
                            },
                        },
                    },
                )

                assert response.status == 200
                payload = await response.json()
            finally:
                await client.close()

        text = payload["result"]["content"][0]["text"]
        assert json.loads(text)["identifier"] == "PYN-1"
        fake_client.create_issue.assert_awaited_once_with(
            team_id="team-1",
            title="Track task",
            description="Task details",
            project_id=None,
            state_id=None,
            label_ids=["label-1"],
            priority=4,
        )

    @pytest.mark.action("linear.issue.read")
    async def test_mcp_get_issue_calls_client(self, monkeypatch):
        monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
        fake_client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        fake_client.get_issue = AsyncMock(
            return_value={
                "id": "issue-1",
                "identifier": "PYN-1",
                "title": "Track task",
                "state": {"id": "state-1", "name": "Human Approved"},
            }
        )
        with patch("pynchy.plugins.integrations.linear.LinearClient", return_value=fake_client):
            client = await start_mcp_client()
            try:
                response = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "linear_get_issue",
                            "arguments": {"issue_id": "issue-1"},
                        },
                    },
                )
                payload = await response.json()
            finally:
                await client.close()

        text = payload["result"]["content"][0]["text"]
        assert json.loads(text)["identifier"] == "PYN-1"
        fake_client.get_issue.assert_awaited_once_with("issue-1")

    @pytest.mark.action("linear.todo.create")
    async def test_mcp_create_workspace_todo_uses_server_workspace(self, monkeypatch):
        monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
        monkeypatch.delenv("LINEAR_TEAM_KEY", raising=False)
        fake_client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        with (
            patch("pynchy.plugins.integrations.linear.LinearClient", return_value=fake_client),
            patch(
                "pynchy.plugins.integrations.linear.create_workspace_todo",
                new=AsyncMock(
                    return_value={
                        "id": "issue-1",
                        "identifier": "SYN-1",
                        "title": "Review docs",
                        "url": "https://linear.app/acme/issue/SYN-1",
                    }
                ),
            ) as create_todo,
        ):
            client = TestClient(TestServer(build_app(workspace="code-improver")))
            await client.start_server()
            try:
                response = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "linear_create_todo",
                            "arguments": {
                                "title": "Review docs",
                                "description": "Evidence and acceptance criteria.",
                                "priority": 4,
                            },
                        },
                    },
                )
                assert response.status == 200
                payload = await response.json()
            finally:
                await client.close()

        assert json.loads(payload["result"]["content"][0]["text"])["identifier"] == "SYN-1"
        create_todo.assert_awaited_once()
        _, args, kwargs = create_todo.mock_calls[0]
        assert args[1].folder == "code-improver"
        proposal = args[2]
        assert proposal.title == "Review docs"
        assert proposal.description == "Evidence and acceptance criteria."
        assert proposal.priority == 4
        assert kwargs["team_key"] is None
        assert kwargs["status"] == "agent_proposed"

    @pytest.mark.action("linear.attachment.create")
    async def test_mcp_attaches_pull_request_to_issue(self, monkeypatch):
        monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
        fake_client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        fake_client.create_attachment = AsyncMock(
            return_value={
                "id": "attachment-1",
                "url": "https://github.com/example/pynchy/pull/85",
            }
        )
        with patch("pynchy.plugins.integrations.linear.LinearClient", return_value=fake_client):
            client = await start_mcp_client()
            try:
                response = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "linear_create_attachment",
                            "arguments": {
                                "issue_id": "issue-1",
                                "url": "https://github.com/example/pynchy/pull/85",
                                "title": "Implement SYN-85",
                                "subtitle": "Ready for review",
                            },
                        },
                    },
                )
                payload = await response.json()
            finally:
                await client.close()

        assert json.loads(payload["result"]["content"][0]["text"])["id"] == "attachment-1"
        fake_client.create_attachment.assert_awaited_once_with(
            "issue-1",
            "https://github.com/example/pynchy/pull/85",
            "Implement SYN-85",
            subtitle="Ready for review",
        )

    @pytest.mark.action("linear.attachment.resolve")
    async def test_mcp_resolves_pull_request_to_attached_issue(self, monkeypatch):
        monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
        fake_client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        fake_client.find_issues_by_attachment_url = AsyncMock(
            return_value=[
                {
                    "id": "attachment-1",
                    "issue": {"id": "issue-1", "identifier": "SYN-85"},
                }
            ]
        )
        with patch("pynchy.plugins.integrations.linear.LinearClient", return_value=fake_client):
            client = await start_mcp_client()
            try:
                response = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "linear_find_issues_by_attachment_url",
                            "arguments": {
                                "url": "https://github.com/example/pynchy/pull/85",
                            },
                        },
                    },
                )
                payload = await response.json()
            finally:
                await client.close()

        linked = json.loads(payload["result"]["content"][0]["text"])
        assert linked[0]["issue"]["identifier"] == "SYN-85"
        fake_client.find_issues_by_attachment_url.assert_awaited_once_with(
            "https://github.com/example/pynchy/pull/85"
        )

    @pytest.mark.action("linear.team.list")
    async def test_mcp_lists_teams_from_the_configured_linear_client(self, monkeypatch):
        monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
        fake_client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        fake_client.list_teams = AsyncMock(return_value=[{"id": "team-1", "name": "Pynchy"}])
        with patch("pynchy.plugins.integrations.linear.LinearClient", return_value=fake_client):
            client = await start_mcp_client()
            try:
                response = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "linear_list_teams", "arguments": {}},
                    },
                )
                payload = await response.json()
            finally:
                await client.close()

        assert json.loads(payload["result"]["content"][0]["text"]) == [
            {"id": "team-1", "name": "Pynchy"}
        ]
        fake_client.list_teams.assert_awaited_once()

    @pytest.mark.action("linear.issue.list")
    async def test_mcp_lists_issues_with_the_requested_team_and_limit(self, monkeypatch):
        monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
        fake_client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        fake_client.list_issues = AsyncMock(return_value=[{"id": "issue-1", "title": "Coverage"}])
        with patch("pynchy.plugins.integrations.linear.LinearClient", return_value=fake_client):
            client = await start_mcp_client()
            try:
                response = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "linear_list_issues",
                            "arguments": {"team_id": "team-1", "first": 7},
                        },
                    },
                )
                payload = await response.json()
            finally:
                await client.close()

        assert json.loads(payload["result"]["content"][0]["text"]) == [
            {"id": "issue-1", "title": "Coverage"}
        ]
        fake_client.list_issues.assert_awaited_once_with(team_id="team-1", first=7)

    @pytest.mark.action("linear.issue.search")
    async def test_mcp_searches_issues_with_the_requested_title_and_limit(self, monkeypatch):
        monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
        fake_client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        fake_client.search_issues = AsyncMock(return_value=[{"id": "issue-1", "title": "Coverage"}])
        with patch("pynchy.plugins.integrations.linear.LinearClient", return_value=fake_client):
            client = await start_mcp_client()
            try:
                response = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "linear_search_issues",
                            "arguments": {"query": "coverage", "team_id": "team-1", "first": 7},
                        },
                    },
                )
                payload = await response.json()
            finally:
                await client.close()

        assert json.loads(payload["result"]["content"][0]["text"]) == [
            {"id": "issue-1", "title": "Coverage"}
        ]
        fake_client.search_issues.assert_awaited_once_with("coverage", team_id="team-1", first=7)

    @pytest.mark.action("linear.todo.list")
    async def test_mcp_lists_workspace_todos_with_the_server_workspace(self, monkeypatch):
        monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
        fake_client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        with (
            patch("pynchy.plugins.integrations.linear.LinearClient", return_value=fake_client),
            patch(
                "pynchy.plugins.integrations.linear.list_workspace_todos",
                new=AsyncMock(return_value=[{"id": "issue-1", "title": "Coverage"}]),
            ) as list_todos,
        ):
            client = TestClient(TestServer(build_app(workspace="code-improver")))
            await client.start_server()
            try:
                response = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "linear_list_todos",
                            "arguments": {"include_done": True},
                        },
                    },
                )
                payload = await response.json()
            finally:
                await client.close()

        assert json.loads(payload["result"]["content"][0]["text"]) == [
            {"id": "issue-1", "title": "Coverage"}
        ]
        _, args, kwargs = list_todos.mock_calls[0]
        assert args[1].folder == "code-improver"
        assert kwargs["include_done"] is True


class TestDocs:
    def test_linear_integration_doc_exists(self):
        doc = Path(__file__).resolve().parent.parent / "docs" / "integrations" / "linear.md"

        assert doc.exists()

    def test_linear_integration_documents_agent_managed_work(self):
        doc = Path(__file__).resolve().parent.parent / "docs" / "integrations" / "linear.md"
        content = doc.read_text()

        assert "immutable workspace marker" in content
        assert "`linear_create_todo` creates an unapproved" in content
        assert "`linear_move_todo`" in content
        assert "`linear_create_comment`" in content
        assert "`linear_create_attachment`" in content
        assert "`linear_find_issues_by_attachment_url`" in content
