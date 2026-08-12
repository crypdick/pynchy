"""Tests for the built-in Linear MCP integration."""

from __future__ import annotations

from io import StringIO
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import make_settings
from rich.console import Console
from rich.traceback import Traceback

from pynchy.config.api import LinearTool
from pynchy.plugins import get_plugin_manager
from pynchy.plugins.integrations.linear import LinearClient, LinearError, LinearMcpPlugin
from pynchy.plugins.integrations.linear_accounts import configured_linear_accounts
from tests.linear_integration_support import (
    FakePostContext,
)


class TestLinearMcpPlugin:
    def test_plugin_provides_script_mcp_server(self):
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

        plugin = LinearMcpPlugin(configured_linear_accounts(settings.tools))
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

        plugin = LinearMcpPlugin(configured_linear_accounts(settings.tools))
        trust = plugin.pynchy_mcp_server_spec()[0].trust

        assert trust is not None
        assert trust.public_source is False
        assert trust.secret_data is False
        assert trust.public_sink is True
        assert trust.dangerous_writes is False

    def test_plugin_isolates_named_accounts_and_their_trust(self):
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

        plugin = LinearMcpPlugin(configured_linear_accounts(settings.tools))
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

    async def test_list_issues_declares_team_filter_as_graphql_id(self):
        response = MagicMock(status=200)
        response.json = AsyncMock(return_value={"data": {"issues": {"nodes": [{"id": "issue-1"}]}}})
        session = MagicMock()
        session.post.return_value = FakePostContext(response)
        client = LinearClient(api_key="lin_api_test", session=session)

        assert await client.list_issues(team_id="team-1", first=1) == [{"id": "issue-1"}]

        _, kwargs = session.post.call_args
        assert kwargs["json"]["variables"] == {"first": 1, "teamId": "team-1"}
        assert "$teamId: ID!" in kwargs["json"]["query"]

    async def test_search_issues_uses_supported_title_filter(self):
        response = MagicMock(status=200)
        response.json = AsyncMock(return_value={"data": {"issues": {"nodes": [{"id": "issue-1"}]}}})
        session = MagicMock()
        session.post.return_value = FakePostContext(response)
        client = LinearClient(api_key="lin_api_test", session=session)

        assert await client.search_issues("orphan reaper", first=1) == [{"id": "issue-1"}]

        _, kwargs = session.post.call_args
        assert kwargs["json"]["variables"] == {"first": 1, "titleQuery": "orphan reaper"}
        assert "containsIgnoreCase: $titleQuery" in kwargs["json"]["query"]
        assert "issueSearch" not in kwargs["json"]["query"]

    async def test_search_issues_combines_title_and_team_filters(self):
        response = MagicMock(status=200)
        response.json = AsyncMock(return_value={"data": {"issues": {"nodes": [{"id": "issue-1"}]}}})
        session = MagicMock()
        session.post.return_value = FakePostContext(response)
        client = LinearClient(api_key="lin_api_test", session=session)

        assert await client.search_issues("orphan reaper", team_id="team-1", first=1) == [
            {"id": "issue-1"}
        ]

        _, kwargs = session.post.call_args
        assert kwargs["json"]["variables"] == {
            "first": 1,
            "titleQuery": "orphan reaper",
            "teamId": "team-1",
        }
        assert "team: { id: { eq: $teamId } }" in kwargs["json"]["query"]

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
            priority=4,
        )

        assert result["identifier"] == "PYN-1"
        assert result["url"] == "https://linear.app/acme/issue/PYN-1"
        _, kwargs = client.query.call_args
        assert kwargs["input"] == {
            "teamId": "team-1",
            "title": "Track tasks",
            "description": "Create task tracker",
            "priority": 4,
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

    async def test_list_issue_comments_normalizes_provider_evidence(self):
        client = LinearClient(api_key="lin_api_test", session=AsyncMock())
        client.query = AsyncMock(
            return_value={
                "issue": {
                    "comments": {
                        "nodes": [
                            {
                                "id": "comment-1",
                                "body": "Validation passed.",
                                "createdAt": "2026-07-25T17:00:00Z",
                                "updatedAt": "2026-07-25T17:00:00Z",
                                "issue": {"id": "issue-1"},
                            }
                        ]
                    }
                }
            }
        )

        comments = await client.list_issue_comments("issue-1")

        assert comments == [
            {
                "id": "comment-1",
                "body": "Validation passed.",
                "createdAt": "2026-07-25T17:00:00Z",
                "updatedAt": "2026-07-25T17:00:00Z",
                "issue": {"id": "issue-1"},
                "issueId": "issue-1",
            }
        ]
        assert client.query.await_args.kwargs == {"issue_id": "issue-1", "first": 100}

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
