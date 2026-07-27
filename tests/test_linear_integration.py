"""Tests for the built-in Linear MCP integration."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import make_settings
from rich.console import Console
from rich.traceback import Traceback

from pynchy.config.models import LinearTool
from pynchy.plugins import get_plugin_manager
from pynchy.plugins.integrations.linear import LinearClient, LinearError, LinearMcpPlugin, build_app


class FakePostContext:
    def __init__(self, response: MagicMock) -> None:
        self.response = response

    async def __aenter__(self) -> MagicMock:
        return self.response

    async def __aexit__(self, exc_type, exc, _tb) -> None:
        return None


async def start_mcp_client() -> TestClient:
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    return client


class TestLinearMcpPlugin:
    def test_plugin_provides_script_mcp_server(self):
        plugin = LinearMcpPlugin()
        settings = make_settings(
            tools={
                "linear": LinearTool(
                    type="linear",
                    public_source=False,
                    secret_data=False,
                    public_sink=True,
                    dangerous_writes=False,
                )
            }
        )

        with patch("pynchy.plugins.integrations.linear.get_settings", return_value=settings):
            spec = plugin.pynchy_mcp_server_spec()[0]

        assert spec.name == "linear"
        assert spec.config.type == "script"
        assert spec.config.command == "uv"
        assert spec.config.args[:2] == ["run", "python"]
        assert spec.config.args[2:] == [
            "-m",
            "pynchy.plugins.integrations.linear",
            "--port",
            "{port}",
            "--workspace",
            "{workspace}",
        ]
        assert spec.config.port == 8474
        assert spec.config.transport == "streamable_http"
        assert spec.config.inject_workspace is True
        assert spec.config.env == {}

    def test_plugin_trust_defaults_allow_linear_task_writes(self):
        plugin = LinearMcpPlugin()
        settings = make_settings(
            tools={
                "linear": LinearTool(
                    type="linear",
                    public_source=False,
                    secret_data=False,
                    public_sink=True,
                    dangerous_writes=False,
                )
            }
        )

        with patch("pynchy.plugins.integrations.linear.get_settings", return_value=settings):
            trust = plugin.pynchy_mcp_server_spec()[0].trust

        assert trust is not None
        assert trust.public_source is False
        assert trust.secret_data is False
        assert trust.public_sink is True
        assert trust.dangerous_writes is False

    def test_plugin_isolates_named_accounts_and_their_trust(self):
        plugin = LinearMcpPlugin()
        settings = make_settings(
            tools={
                "linear_public": LinearTool(
                    type="linear",
                    required_env=["LINEAR_PUBLIC_API_KEY"],  # pragma: allowlist secret
                    public_source=True,
                    secret_data=False,
                    public_sink=True,
                    dangerous_writes=False,
                ),
                "linear_synapse": LinearTool(
                    type="linear",
                    required_env=["LINEAR_SYNAPSE_API_KEY"],  # pragma: allowlist secret
                    public_source=False,
                    secret_data=True,
                    public_sink=False,
                    dangerous_writes=False,
                ),
            }
        )

        with patch("pynchy.plugins.integrations.linear.get_settings", return_value=settings):
            specs = plugin.pynchy_mcp_server_spec()

        assert [spec.name for spec in specs] == ["linear_public", "linear_synapse"]
        assert specs[0].config.env == {}
        assert specs[0].trust is not None
        assert specs[0].trust.public_source is True
        assert specs[1].config.env == {}
        assert specs[1].trust is not None
        assert specs[1].trust.public_source is False
        assert specs[1].trust.secret_data is True
        assert specs[1].trust.public_sink is False
        assert specs[1].trust.dangerous_writes is False

    def test_plugin_is_registered(self):
        pm = get_plugin_manager()

        assert isinstance(pm.get_plugin("builtin-linear"), LinearMcpPlugin)


class TestLinearClient:
    async def test_query_sends_linear_authorization_header(self):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json = AsyncMock(return_value={"data": {"viewer": {"name": "Alice"}}})
        session = MagicMock()
        session.post.return_value = FakePostContext(response)
        client = LinearClient(api_key="lin_api_test", session=session)

        result = await client.query("query Viewer { viewer { name } }")

        assert result == {"viewer": {"name": "Alice"}}
        session.post.assert_called_once()
        _, kwargs = session.post.call_args
        assert kwargs["headers"]["Authorization"] == "lin_api_test"
        assert kwargs["headers"]["Content-Type"] == "application/json"
        assert kwargs["json"] == {"query": "query Viewer { viewer { name } }", "variables": {}}

    async def test_query_raises_graphql_errors(self):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json = AsyncMock(return_value={"errors": [{"message": "Nope"}]})
        session = MagicMock()
        session.post.return_value = FakePostContext(response)
        client = LinearClient(api_key="lin_api_test", session=session)

        with pytest.raises(LinearError, match="Nope"):
            await client.query("query Broken { viewer { id } }")

    async def test_http_error_does_not_embed_the_authorization_header(self):
        response = MagicMock(status=400)
        response.json = AsyncMock(return_value={"errors": [{"message": "Query is invalid"}]})
        session = MagicMock()
        session.post.return_value = FakePostContext(response)
        client = LinearClient(api_key="lin_api_test_must_not_leak", session=session)

        with pytest.raises(LinearError) as exc_info:
            await client.query("query Broken { viewer { id } }")

        assert "Query is invalid" in str(exc_info.value)
        assert "lin_api_test_must_not_leak" not in str(exc_info.value)
        response.raise_for_status.assert_not_called()

    async def test_graphql_error_traceback_does_not_render_authorization_header(self):
        response = MagicMock(status=400)
        response.json = AsyncMock(return_value={"errors": [{"message": "Query is invalid"}]})
        session = MagicMock()
        session.post.return_value = FakePostContext(response)
        client = LinearClient(api_key="lin_api_traceback_must_not_leak", session=session)

        with pytest.raises(LinearError) as exc_info:
            await client.query("query Broken { viewer { id } }")

        client_traceback = exc_info.value.__traceback__
        while client_traceback and client_traceback.tb_frame.f_code.co_filename == __file__:
            client_traceback = client_traceback.tb_next
        assert client_traceback is not None
        rendered = StringIO()
        Console(file=rendered, color_system=None, width=200).print(
            Traceback.from_exception(
                type(exc_info.value),
                exc_info.value,
                client_traceback,
                show_locals=True,
            )
        )

        assert "lin_api_traceback_must_not_leak" not in rendered.getvalue()

    async def test_list_teams_flattens_nodes(self):
        client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        client.query = AsyncMock(
            return_value={
                "teams": {
                    "nodes": [
                        {"id": "team-1", "key": "PYN", "name": "Pynchy"},
                    ]
                }
            }
        )

        assert await client.list_teams() == [{"id": "team-1", "key": "PYN", "name": "Pynchy"}]

    async def test_create_issue_returns_identifier_and_url(self):
        client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        client.query = AsyncMock(
            return_value={
                "issueCreate": {
                    "success": True,
                    "issue": {
                        "id": "issue-1",
                        "identifier": "PYN-1",
                        "title": "Track tasks",
                        "url": "https://linear.app/acme/issue/PYN-1",
                    },
                }
            }
        )

        result = await client.create_issue(
            team_id="team-1",
            title="Track tasks",
            description="Create task tracker",
        )

        assert result["identifier"] == "PYN-1"
        assert result["url"] == "https://linear.app/acme/issue/PYN-1"
        _, kwargs = client.query.call_args
        assert kwargs["input"] == {
            "teamId": "team-1",
            "title": "Track tasks",
            "description": "Create task tracker",
        }

    async def test_create_issue_omits_absent_optional_fields(self):
        client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        client.query = AsyncMock(
            return_value={
                "issueCreate": {
                    "success": True,
                    "issue": {"id": "issue-1", "identifier": "PYN-1"},
                }
            }
        )

        await client.create_issue(team_id="team-1", title="Track tasks")

        _, kwargs = client.query.call_args
        assert kwargs["input"] == {"teamId": "team-1", "title": "Track tasks"}

    async def test_create_comment_returns_provider_comment(self):
        client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        client.query = AsyncMock(
            return_value={
                "commentCreate": {
                    "success": True,
                    "comment": {
                        "id": "comment-1",
                        "body": "Validation passed.",
                        "createdAt": "2026-07-25T17:00:00Z",
                        "updatedAt": "2026-07-25T17:00:00Z",
                        "issue": {"id": "issue-1"},
                    },
                }
            }
        )

        result = await client.create_comment("issue-1", "Validation passed.")

        assert result["id"] == "comment-1"
        assert result["issueId"] == "issue-1"
        assert result["updatedAt"] == "2026-07-25T17:00:00Z"
        assert client.query.await_args.kwargs == {
            "issue_id": "issue-1",
            "body": "Validation passed.",
        }

    async def test_create_attachment_returns_visible_issue_attachment(self):
        client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        client.query = AsyncMock(
            return_value={
                "attachmentCreate": {
                    "success": True,
                    "attachment": {
                        "id": "attachment-1",
                        "url": "https://github.com/example/pynchy/pull/85",
                        "title": "Implement SYN-85",
                    },
                }
            }
        )

        result = await client.create_attachment(
            "issue-1",
            "https://github.com/example/pynchy/pull/85",
            "Implement SYN-85",
            subtitle="Ready for review",
        )

        assert result["id"] == "attachment-1"
        assert client.query.await_args.kwargs == {
            "issue_id": "issue-1",
            "url": "https://github.com/example/pynchy/pull/85",
            "title": "Implement SYN-85",
            "subtitle": "Ready for review",
        }

    async def test_find_issues_by_attachment_url_returns_linked_issues(self):
        client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        client.query = AsyncMock(
            return_value={
                "attachmentsForURL": {
                    "nodes": [
                        {
                            "id": "attachment-1",
                            "url": "https://github.com/example/pynchy/pull/85",
                            "issue": {"id": "issue-1", "identifier": "SYN-85"},
                        }
                    ]
                }
            }
        )

        result = await client.find_issues_by_attachment_url(
            "https://github.com/example/pynchy/pull/85"
        )

        assert result[0]["issue"]["identifier"] == "SYN-85"
        assert client.query.await_args.kwargs == {
            "url": "https://github.com/example/pynchy/pull/85"
        }

    async def test_get_issue_returns_none_after_canary_cleanup(self):
        client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        client.query = AsyncMock(return_value={"issue": None})

        assert await client.get_issue("issue-1") is None

    async def test_get_issue_returns_none_when_linear_reports_a_deleted_issue(self):
        client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        client.query = AsyncMock(side_effect=LinearError("Entity not found: Issue"))

        assert await client.get_issue("issue-1") is None

    async def test_delete_issue_requires_provider_success(self):
        client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        client.query = AsyncMock(return_value={"issueDelete": {"success": True}})

        await client.delete_issue("issue-1")

        assert client.query.await_args.kwargs == {"issue_id": "issue-1"}


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
                "linear_get_issue",
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
            label_ids=None,
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
