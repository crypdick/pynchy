"""Built-in Linear MCP server plugin.

The plugin registers a host-side script MCP server that gives agents a small,
task-tracking-focused Linear surface: discover teams, list issues, and create
issues. Existing backlog migration can build on this instead of embedding a
separate Linear client into backlog-specific scripts.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

import aiohttp
import pluggy
from aiohttp import web

from pynchy.logger import logger
from pynchy.plugins.integrations.linear_boards import (
    create_workspace_todo,
    list_workspace_todos,
    move_workspace_todo,
)
from pynchy.plugins.integrations.linear_tools import tool_specs

hookimpl = pluggy.HookimplMarker("pynchy")

LINEAR_API_URL = "https://api.linear.app/graphql"
DEFAULT_PORT = 8474
WORKSPACE_APP_KEY = web.AppKey("workspace", object)


class LinearError(RuntimeError):
    """Raised when Linear returns GraphQL errors or an unexpected payload."""


@dataclass(frozen=True)
class WorkspaceContext:
    """Minimal workspace identity passed to Linear board helpers."""

    folder: str
    name: str
    jid: str = ""


class LinearClient:
    """Tiny async Linear GraphQL client for task-tracking MCP tools."""

    def __init__(
        self,
        *,
        api_key: str,
        session: Any,
        endpoint: str = LINEAR_API_URL,
    ) -> None:
        self._api_key = api_key
        self._session = session
        self._endpoint = endpoint

    async def query(self, query: str, **variables: Any) -> dict[str, Any]:
        payload = {"query": query, "variables": variables}
        headers = {
            "Authorization": self._api_key,
            "Content-Type": "application/json",
        }
        async with self._session.post(self._endpoint, json=payload, headers=headers) as response:
            response.raise_for_status()
            body = await response.json()

        if errors := body.get("errors"):
            messages = "; ".join(str(error.get("message", error)) for error in errors)
            raise LinearError(messages)
        data = body.get("data")
        if not isinstance(data, dict):
            raise LinearError("Linear response did not include a data object")
        return data

    async def list_teams(self) -> list[dict[str, Any]]:
        data = await self.query(
            """
            query ListTeams {
              teams {
                nodes { id key name }
              }
            }
            """
        )
        return _nodes(data, "teams")

    async def list_issues(
        self,
        *,
        team_id: str | None = None,
        first: int = 50,
    ) -> list[dict[str, Any]]:
        if team_id:
            data = await self.query(
                """
                query ListTeamIssues($first: Int!, $teamId: String!) {
                  issues(first: $first, filter: { team: { id: { eq: $teamId } } }) {
                    nodes {
                      id identifier title url priority createdAt updatedAt
                      state { id name type }
                      team { id key name }
                      project { id name }
                    }
                  }
                }
                """,
                first=first,
                teamId=team_id,
            )
        else:
            data = await self.query(
                """
                query ListIssues($first: Int!) {
                  issues(first: $first) {
                    nodes {
                      id identifier title url priority createdAt updatedAt
                      state { id name type }
                      team { id key name }
                      project { id name }
                    }
                  }
                }
                """,
                first=first,
            )
        return _nodes(data, "issues")

    async def create_issue(
        self,
        *,
        team_id: str,
        title: str,
        description: str | None = None,
        project_id: str | None = None,
        state_id: str | None = None,
        label_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        data = await self.query(
            """
            mutation CreateIssue(
              $team_id: String!,
              $title: String!,
              $description: String,
              $project_id: String,
              $state_id: String,
              $label_ids: [String!]
            ) {
              issueCreate(input: {
                teamId: $team_id,
                title: $title,
                description: $description,
                projectId: $project_id,
                stateId: $state_id,
                labelIds: $label_ids
              }) {
                success
                issue { id identifier title url }
              }
            }
            """,
            team_id=team_id,
            title=title,
            description=description,
            project_id=project_id,
            state_id=state_id,
            label_ids=label_ids,
        )
        result = data.get("issueCreate")
        if not isinstance(result, dict) or not result.get("success"):
            raise LinearError("Linear did not create the issue")
        issue = result.get("issue")
        if not isinstance(issue, dict):
            raise LinearError("Linear issueCreate response did not include an issue")
        return issue


def _nodes(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    connection = data.get(key)
    if not isinstance(connection, dict):
        raise LinearError(f"Linear response did not include {key}")
    nodes = connection.get("nodes")
    if not isinstance(nodes, list):
        raise LinearError(f"Linear response did not include {key}.nodes")
    return [node for node in nodes if isinstance(node, dict)]


class LinearMcpPlugin:
    """Register the built-in Linear script MCP server."""

    @hookimpl
    def pynchy_mcp_server_spec(self) -> dict[str, Any]:
        return {
            "name": "linear",
            "type": "script",
            "command": "uv",
            "args": [
                "run",
                "python",
                "-m",
                "pynchy.plugins.integrations.linear",
                "--port",
                "{port}",
                "--workspace",
                "{workspace}",
            ],
            "port": DEFAULT_PORT,
            "transport": "streamable_http",
            "idle_timeout": 600,
            "inject_workspace": True,
            "env_forward": {"LINEAR_API_KEY": "LINEAR_API_KEY"},  # pragma: allowlist secret
            "trust": {
                "public_source": False,
                "secret_data": False,
                "public_sink": True,
                "dangerous_writes": False,
            },
        }


def build_app(*, workspace: str | None = None) -> Any:
    app = web.Application()
    app[WORKSPACE_APP_KEY] = workspace
    app.router.add_get("/", _handle_health)
    app.router.add_post("/mcp", _handle_mcp)
    return app


async def _handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "pynchy-linear"})


async def _handle_mcp(request: web.Request) -> web.Response:
    payload = await request.json()
    request_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}

    try:
        if method == "initialize":
            return _jsonrpc_result(request_id, _initialize_result())
        if method == "notifications/initialized":
            return web.Response(status=202)
        if method == "tools/list":
            return _jsonrpc_result(request_id, {"tools": tool_specs()})
        if method == "tools/call":
            return _jsonrpc_result(
                request_id,
                await _call_tool(
                    params, workspace=cast("str | None", request.app[WORKSPACE_APP_KEY])
                ),
            )
        return _jsonrpc_error(request_id, -32601, f"Unknown MCP method: {method}")
    except Exception as exc:
        logger.exception("Linear MCP request failed", method=method)
        return _jsonrpc_result(
            request_id, _text_result(f"Linear tool failed: {exc}", is_error=True)
        )


def _initialize_result() -> dict[str, Any]:
    return {
        "protocolVersion": "2025-06-18",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "pynchy-linear", "version": "0.1.0"},
    }


async def _call_tool(params: dict[str, Any], *, workspace: str | None = None) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _text_result("Tool arguments must be an object", is_error=True)

    token = os.environ.get("LINEAR_API_KEY")
    if not token:
        return _text_result("LINEAR_API_KEY is not configured", is_error=True)

    async with aiohttp.ClientSession() as session:
        client = LinearClient(api_key=token, session=session)
        handlers: dict[
            str,
            Callable[[LinearClient, dict[str, Any], str | None], Awaitable[Any]],
        ] = {
            "linear_list_teams": _tool_list_teams,
            "linear_list_issues": _tool_list_issues,
            "linear_create_issue": _tool_create_issue,
            "linear_list_todos": _tool_list_todos,
            "linear_create_todo": _tool_create_todo,
            "linear_move_todo": _tool_move_todo,
        }
        handler = handlers.get(str(name))
        if handler is None:
            return _text_result(f"Unknown Linear tool: {name}", is_error=True)
        result = await handler(client, arguments, workspace)
    return _json_result(result)


async def _tool_list_teams(
    client: Any,
    arguments: dict[str, Any],
    workspace: str | None,
) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", await client.list_teams())


async def _tool_list_issues(
    client: Any,
    arguments: dict[str, Any],
    workspace: str | None,
) -> list[dict[str, Any]]:
    first = arguments.get("first", 50)
    if not isinstance(first, int):
        first = int(first)
    return cast(
        "list[dict[str, Any]]",
        await client.list_issues(team_id=arguments.get("team_id"), first=first),
    )


async def _tool_create_issue(
    client: Any,
    arguments: dict[str, Any],
    workspace: str | None,
) -> dict[str, Any]:
    team_id = _required_str(arguments, "team_id")
    title = _required_str(arguments, "title")
    label_ids = arguments.get("label_ids")
    if label_ids is not None and not isinstance(label_ids, list):
        raise LinearError("label_ids must be an array of Linear label ids")
    return cast(
        "dict[str, Any]",
        await client.create_issue(
            team_id=team_id,
            title=title,
            description=arguments.get("description"),
            project_id=arguments.get("project_id"),
            state_id=arguments.get("state_id"),
            label_ids=label_ids,
        ),
    )


async def _tool_list_todos(
    client: Any,
    arguments: dict[str, Any],
    workspace: str | None,
) -> list[dict[str, Any]]:
    return await list_workspace_todos(
        client,
        _workspace_context(workspace),
        team_key=os.environ.get("LINEAR_TEAM_KEY"),
        include_done=bool(arguments.get("include_done", False)),
    )


async def _tool_create_todo(
    client: Any,
    arguments: dict[str, Any],
    workspace: str | None,
) -> dict[str, Any]:
    return await create_workspace_todo(
        client,
        _workspace_context(workspace),
        _required_str(arguments, "title"),
        team_key=os.environ.get("LINEAR_TEAM_KEY"),
        status=str(arguments.get("status", "backlog")),
    )


async def _tool_move_todo(
    client: Any,
    arguments: dict[str, Any],
    workspace: str | None,
) -> dict[str, Any]:
    return await move_workspace_todo(
        client,
        _workspace_context(workspace),
        issue_id=_required_str(arguments, "issue_id"),
        status=_required_str(arguments, "status"),
        team_key=os.environ.get("LINEAR_TEAM_KEY"),
    )


def _workspace_context(workspace: str | None) -> WorkspaceContext:
    if not workspace:
        raise LinearError("Workspace-scoped Linear todo tools require an MCP workspace instance")
    name = workspace.replace("-", " ").replace("_", " ").title()
    return WorkspaceContext(folder=workspace, name=name)


def _required_str(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LinearError(f"{key} is required")
    return value


def _json_result(value: Any) -> dict[str, Any]:
    return _text_result(json.dumps(value, indent=2, sort_keys=True))


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def _jsonrpc_result(request_id: Any, result: dict[str, Any]) -> web.Response:
    return web.json_response({"jsonrpc": "2.0", "id": request_id, "result": result})


def _jsonrpc_error(request_id: Any, code: int, message: str) -> web.Response:
    return web.json_response(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Pynchy Linear MCP server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", DEFAULT_PORT)))
    parser.add_argument("--workspace")
    args = parser.parse_args(argv)
    web.run_app(build_app(workspace=args.workspace), host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
