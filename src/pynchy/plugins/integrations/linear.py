"""Built-in Linear MCP server plugin.

The plugin registers a host-side script MCP server that gives agents a small,
task-tracking-focused Linear surface: discover teams, list issues, and create
issues through the shared Linear client and board helpers.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import (  # noqa: TC003 - beartype resolves these runtime annotations.
    Awaitable,
    Callable,
)
from dataclasses import dataclass
from typing import Any, cast

import aiohttp
import pluggy
from aiohttp import web

from pynchy.logger import logger
from pynchy.plugins.api import (
    # beartype resolves the hook return annotation at runtime.
    # beartype resolves the hook parameter annotation at runtime.
    ComputerUseBackend,
    HostActionRegistration,
    McpServerConfig,
    McpServerSpec,
    WebhookRoute,  # beartype resolves the hook return annotation at runtime.
)
from pynchy.plugins.integrations.linear_accounts import (  # noqa: TC001 - beartype resolves plugin configuration annotations at runtime.
    LinearAccount,
)
from pynchy.plugins.integrations.linear_boards import (
    WorkspaceTodoProposal,
    create_workspace_todo,
    list_workspace_todos,
)
from pynchy.plugins.integrations.linear_client import LinearClient, LinearError
from pynchy.plugins.integrations.linear_session_reset import (
    LinearSessionResetState,
    cancel_linear_execution_for_reset,
)
from pynchy.plugins.integrations.linear_statuses import AGENT_PROPOSED_STATUS
from pynchy.plugins.integrations.linear_tools import tool_specs
from pynchy.plugins.integrations.linear_webhooks import linear_webhook_routes
from pynchy.plugins.integrations.linear_work_item_actions import host_action_registration
from pynchy.workspace.api import (
    ServiceTrustConfig,
    WorkspaceProfile,
)

hookimpl = pluggy.HookimplMarker("pynchy")

DEFAULT_PORT = 8474
LOCAL_MCP_BIND_HOST = "localhost"
WORKSPACE_APP_KEY = web.AppKey("workspace", object)
_LINEAR_LABEL_IDS_NOT_ARRAY = "label_ids must be an array of Linear label ids"
_LINEAR_WORKSPACE_REQUIRED = "Workspace-scoped Linear todo tools require an MCP workspace instance"
_LINEAR_REQUIRED_ARGUMENT = "{key} is required"
_LINEAR_DESCRIPTION_NOT_STRING = "description must be a string"
_LINEAR_SEARCH_LIMIT_INVALID = "first must be an integer from 1 through 100"
_LINEAR_TEAM_ID_NOT_STRING = "team_id must be a string"
# NOTE: Keep docs/integrations/linear.md priority mapping aligned with this contract.
_LINEAR_PRIORITY_INVALID = "priority must be an integer from 0 through 4"


@dataclass(frozen=True)
class WorkspaceContext:
    """Minimal workspace identity passed to Linear board helpers."""

    folder: str
    name: str
    jid: str = ""


@dataclass(frozen=True)
class _SearchIssuesArguments:
    query: str
    team_id: str | None
    first: int

    @classmethod
    def parse(cls, arguments: dict[str, Any]) -> _SearchIssuesArguments:
        unexpected = sorted(arguments.keys() - {"query", "team_id", "first"})
        if unexpected:
            raise LinearError(f"unexpected arguments: {', '.join(unexpected)}")
        first = arguments.get("first", 50)
        if type(first) is not int or not 1 <= first <= 100:
            raise LinearError(_LINEAR_SEARCH_LIMIT_INVALID)
        team_id = arguments.get("team_id")
        if team_id is not None and not isinstance(team_id, str):
            raise LinearError(_LINEAR_TEAM_ID_NOT_STRING)
        return cls(query=_required_str(arguments, "query"), team_id=team_id, first=first)


class LinearMcpPlugin:
    """Register the built-in Linear script MCP server."""

    def __init__(
        self,
        accounts: tuple[LinearAccount, ...] = (),
        *,
        cancel_scheduled_workflow: Callable[[str], Awaitable[bool]] | None = None,
        session_reset_state: LinearSessionResetState | None = None,
    ) -> None:
        self._accounts = accounts
        self._cancel_scheduled_workflow = cancel_scheduled_workflow
        self._session_reset_state = session_reset_state

    def configure(
        self,
        accounts: tuple[LinearAccount, ...],
        *,
        cancel_scheduled_workflow: Callable[[str], Awaitable[bool]],
        session_reset_state: LinearSessionResetState,
    ) -> None:
        """Apply the resolved Linear accounts before MCP specs are collected."""
        self._accounts = accounts
        self._cancel_scheduled_workflow = cancel_scheduled_workflow
        self._session_reset_state = session_reset_state

    @hookimpl
    def pynchy_mcp_server_spec(self) -> tuple[McpServerSpec, ...]:
        """Create one isolated MCP server definition per configured account."""
        return tuple(
            McpServerSpec(
                name=account.name,
                config=McpServerConfig(
                    type="script",
                    command="uv",
                    args=[
                        "run",
                        "python",
                        "-m",
                        "pynchy.plugins.integrations.linear",
                        "--port",
                        "{port}",
                        "--workspace",
                        "{workspace}",
                    ],
                    port=DEFAULT_PORT,
                    transport="streamable_http",
                    idle_timeout=600,
                    inject_workspace=True,
                ),
                trust=ServiceTrustConfig(
                    public_source=account.config.public_source,
                    secret_data=account.config.secret_data,
                    public_sink=account.config.public_sink,
                    dangerous_writes=account.config.dangerous_writes,
                ),
            )
            for account in self._accounts
        )

    @hookimpl
    def pynchy_service_handler(
        self, computer_use_backends: tuple[ComputerUseBackend, ...]
    ) -> HostActionRegistration:
        """Keep durable work-item lifecycle writes in the host process."""
        del computer_use_backends
        return host_action_registration()

    @hookimpl
    def pynchy_webhook_routes(self) -> tuple[WebhookRoute, ...]:
        """Expose configured Linear webhook subscriptions to the host ingress."""
        return linear_webhook_routes()

    @hookimpl
    async def pynchy_before_context_reset(self, group: WorkspaceProfile) -> None:
        """Settle Linear execution ownership before a session is cleared."""
        if self._cancel_scheduled_workflow is None or self._session_reset_state is None:
            raise RuntimeError("Linear plugin was not configured for context-reset settlement")
        await cancel_linear_execution_for_reset(
            group,
            cancel_scheduled_workflow=self._cancel_scheduled_workflow,
            state=self._session_reset_state,
        )


def build_app(*, workspace: str | None = None) -> object:
    app = web.Application()
    app[WORKSPACE_APP_KEY] = workspace
    app.router.add_get("/", _handle_health)
    app.router.add_post("/mcp", _handle_mcp)
    return app


async def _handle_health(_request: web.Request) -> web.Response:  # noqa: RUF029 - aiohttp route handlers are async.
    return web.json_response({"status": "ok", "service": "pynchy-linear"})


async def _handle_mcp(request: web.Request) -> web.StreamResponse:
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
    except Exception as exc:  # noqa: BLE001 - Linear MCP tool failures are converted to JSON-RPC errors.
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
            str, Callable[[LinearClient, dict[str, Any], str | None], Awaitable[Any]]
        ] = {
            "linear_list_teams": _tool_list_teams,
            "linear_list_issues": _tool_list_issues,
            "linear_search_issues": _tool_search_issues,
            "linear_get_issue": _tool_get_issue,
            "linear_create_issue": _tool_create_issue,
            "linear_list_todos": _tool_list_todos,
            "linear_create_todo": _tool_create_todo,
            "linear_create_attachment": _tool_create_attachment,
            "linear_find_issues_by_attachment_url": _tool_find_issues_by_attachment_url,
        }
        handler = handlers.get(str(name))
        if handler is None:
            return _text_result(f"Unknown Linear tool: {name}", is_error=True)
        result = await handler(client, arguments, workspace)
    return _json_result(result)


async def _tool_list_teams(
    client: LinearClient,
    _arguments: dict[str, Any],
    _workspace: str | None,
) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", await cast("Any", client).list_teams())


async def _tool_list_issues(
    client: LinearClient,
    arguments: dict[str, Any],
    _workspace: str | None,
) -> list[dict[str, Any]]:
    first = arguments.get("first", 50)
    if not isinstance(first, int):
        first = int(first)
    return cast(
        "list[dict[str, Any]]",
        await cast("Any", client).list_issues(team_id=arguments.get("team_id"), first=first),
    )


async def _tool_search_issues(
    client: LinearClient,
    arguments: dict[str, Any],
    _workspace: str | None,
) -> list[dict[str, Any]]:
    parsed = _SearchIssuesArguments.parse(arguments)
    return cast(
        "list[dict[str, Any]]",
        await cast("Any", client).search_issues(
            parsed.query,
            team_id=parsed.team_id,
            first=parsed.first,
        ),
    )


async def _tool_get_issue(
    client: LinearClient,
    arguments: dict[str, Any],
    _workspace: str | None,
) -> dict[str, Any] | None:
    issue_id = _required_str(arguments, "issue_id")
    return cast("dict[str, Any] | None", await cast("Any", client).get_issue(issue_id))


async def _tool_create_issue(
    client: LinearClient,
    arguments: dict[str, Any],
    _workspace: str | None,
) -> dict[str, Any]:
    team_id = _required_str(arguments, "team_id")
    title = _required_str(arguments, "title")
    label_ids = arguments.get("label_ids")
    if label_ids is not None and not isinstance(label_ids, list):
        raise LinearError(_LINEAR_LABEL_IDS_NOT_ARRAY)
    priority = arguments.get("priority")
    if priority is not None and (type(priority) is not int or not 0 <= priority <= 4):
        raise LinearError(_LINEAR_PRIORITY_INVALID)
    return cast(
        "dict[str, Any]",
        await cast("Any", client).create_issue(
            team_id=team_id,
            title=title,
            description=arguments.get("description"),
            project_id=arguments.get("project_id"),
            state_id=None,
            label_ids=label_ids,
            priority=priority,
        ),
    )


async def _tool_list_todos(
    client: LinearClient,
    arguments: dict[str, Any],
    workspace: str | None,
) -> list[dict[str, Any]]:
    return await list_workspace_todos(
        client,
        _workspace_context(workspace),
        team_key=os.environ.get("LINEAR_TEAM_KEY"),
        include_done=bool(arguments.get("include_done")),
    )


async def _tool_create_todo(
    client: LinearClient,
    arguments: dict[str, Any],
    workspace: str | None,
) -> dict[str, Any]:
    description = arguments.get("description")
    if description is not None and not isinstance(description, str):
        raise LinearError(_LINEAR_DESCRIPTION_NOT_STRING)
    priority = arguments.get("priority")
    if priority is not None and (type(priority) is not int or not 0 <= priority <= 4):
        raise LinearError(_LINEAR_PRIORITY_INVALID)
    return await create_workspace_todo(
        client,
        _workspace_context(workspace),
        WorkspaceTodoProposal(
            title=_required_str(arguments, "title"),
            description=description,
            priority=priority,
        ),
        team_key=os.environ.get("LINEAR_TEAM_KEY"),
        status=AGENT_PROPOSED_STATUS,
    )


async def _tool_create_attachment(
    client: LinearClient,
    arguments: dict[str, Any],
    _workspace: str | None,
) -> dict[str, Any]:
    subtitle = arguments.get("subtitle")
    if subtitle is not None and not isinstance(subtitle, str):
        raise LinearError("Linear argument subtitle must be a string")
    return await client.create_attachment(
        _required_str(arguments, "issue_id"),
        _required_str(arguments, "url"),
        _required_str(arguments, "title"),
        subtitle=subtitle,
    )


async def _tool_find_issues_by_attachment_url(
    client: LinearClient,
    arguments: dict[str, Any],
    _workspace: str | None,
) -> list[dict[str, Any]]:
    return await client.find_issues_by_attachment_url(_required_str(arguments, "url"))


def _workspace_context(workspace: str | None) -> WorkspaceContext:
    if not workspace:
        raise LinearError(_LINEAR_WORKSPACE_REQUIRED)
    name = workspace.replace("-", " ").replace("_", " ").title()
    return WorkspaceContext(folder=workspace, name=name)


def _required_str(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LinearError(_LINEAR_REQUIRED_ARGUMENT.format(key=key))
    return value


def _json_result(value: object) -> dict[str, Any]:
    return _text_result(json.dumps(cast("Any", value), indent=2, sort_keys=True))


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def _jsonrpc_result(request_id: object, result: dict[str, Any]) -> web.Response:
    return web.json_response({"jsonrpc": "2.0", "id": request_id, "result": result})


def _jsonrpc_error(request_id: object, code: int, message: str) -> web.Response:
    return web.json_response(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Pynchy Linear MCP server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", DEFAULT_PORT)))
    parser.add_argument("--workspace")
    args = parser.parse_args(argv)
    web.run_app(
        cast("web.Application", build_app(workspace=args.workspace)),
        host=LOCAL_MCP_BIND_HOST,
        port=args.port,
    )


if __name__ == "__main__":
    main()
