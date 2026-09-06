"""Built-in Linear MCP server plugin.

The plugin registers a host-side script MCP server that gives agents a small,
task-tracking-focused Linear surface: discover teams, list issues, and create
issues through the shared Linear client and board helpers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import (
    Awaitable,
    Callable,
)
from dataclasses import dataclass
from typing import Literal

import aiohttp
import pluggy
from aiohttp import web
from pydantic import BaseModel, ConfigDict, ValidationError

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
from pynchy.plugins.integrations.linear_accounts import (
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
from pynchy.plugins.integrations.linear_tools import (
    CreateAttachmentCall,
    CreateIssueCall,
    CreateTodoCall,
    GetIssueCall,
    LinearToolArgumentsError,
    LinearToolCall,
    ListIssuesCall,
    ListTeamsCall,
    ListTodosCall,
    SearchIssuesCall,
    UnknownLinearToolError,
    parse_tool_call,
    tool_specs,
)
from pynchy.plugins.integrations.linear_webhooks import linear_webhook_routes
from pynchy.plugins.integrations.linear_work_item_actions import host_action_registration
from pynchy.workspace.api import (
    ServiceTrustConfig,
    WorkspaceProfile,
)

hookimpl = pluggy.HookimplMarker("pynchy")

DEFAULT_PORT = 8474
LOCAL_MCP_BIND_HOST = "localhost"
WORKSPACE_APP_KEY: web.AppKey[str | None] = web.AppKey("workspace")
_LINEAR_WORKSPACE_REQUIRED = "Workspace-scoped Linear todo tools require an MCP workspace instance"


@dataclass(frozen=True)
class WorkspaceContext:
    """Minimal workspace identity passed to Linear board helpers."""

    folder: str
    name: str
    jid: str = ""


class _McpRequest(BaseModel):
    """JSON-RPC fields accepted at the Linear HTTP boundary."""

    model_config = ConfigDict(extra="forbid")

    jsonrpc: Literal["2.0"]  # noqa: V107
    id: int | str | None = None
    method: str
    params: object = None


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
                    command=sys.executable,
                    args=[
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
                    # Concurrent host imports can consume nearly five seconds before HTTP binds.
                    startup_timeout_seconds=10,
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


def build_app(*, workspace: str | None = None) -> web.Application:
    app = web.Application()
    app[WORKSPACE_APP_KEY] = workspace
    app.router.add_get("/", _handle_health)
    app.router.add_post("/mcp", _handle_mcp)
    return app


async def _handle_health(_request: web.Request) -> web.Response:  # noqa: RUF029 - aiohttp route handlers are async.
    return web.json_response({"status": "ok", "service": "pynchy-linear"})


async def _handle_mcp(  # noqa: PLR0911 - one direct response per JSON-RPC method.
    request: web.Request,
) -> web.StreamResponse:
    try:
        payload = _McpRequest.model_validate(await request.json())
    except ValidationError as exc:
        return _jsonrpc_error(None, -32600, f"Invalid JSON-RPC request: {exc}")

    try:
        if payload.method == "initialize":
            return _jsonrpc_result(payload.id, _initialize_result())
        if payload.method == "notifications/initialized":
            return web.Response(status=202)
        if payload.method == "tools/list":
            return _jsonrpc_result(payload.id, {"tools": tool_specs()})
        if payload.method == "tools/call":
            return _jsonrpc_result(
                payload.id,
                await _call_tool(
                    payload.params,
                    workspace=request.app[WORKSPACE_APP_KEY],
                ),
            )
        return _jsonrpc_error(payload.id, -32601, f"Unknown MCP method: {payload.method}")
    except Exception as exc:  # noqa: BLE001 - Linear MCP tool failures are converted to JSON-RPC errors.
        logger.exception("Linear MCP request failed", method=payload.method)
        return _jsonrpc_result(
            payload.id, _text_result(f"Linear tool failed: {exc}", is_error=True)
        )


def _initialize_result() -> dict[str, object]:
    return {
        "protocolVersion": "2025-06-18",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "pynchy-linear", "version": "0.1.0"},
    }


async def _call_tool(params: object, *, workspace: str | None = None) -> dict[str, object]:
    try:
        call = parse_tool_call(params)
    except UnknownLinearToolError as exc:
        return _text_result(f"Unknown Linear tool: {exc}", is_error=True)
    except LinearToolArgumentsError as exc:
        return _text_result(str(exc), is_error=True)

    token = os.environ.get("LINEAR_API_KEY")
    if not token:
        return _text_result("LINEAR_API_KEY is not configured", is_error=True)

    async with aiohttp.ClientSession() as session:
        client = LinearClient(api_key=token, session=session)
        result = await _execute_tool(client, call, workspace)
    return _json_result(result)


async def _execute_tool(  # noqa: PLR0911 - discriminated calls each execute one operation.
    client: LinearClient,
    call: LinearToolCall,
    workspace: str | None,
) -> object:
    match call:
        case ListTeamsCall():
            return await client.list_teams()
        case ListIssuesCall(arguments=arguments):
            return await client.list_issues(team_id=arguments.team_id, first=arguments.first)
        case SearchIssuesCall(arguments=arguments):
            return await client.search_issues(
                arguments.query,
                team_id=arguments.team_id,
                first=arguments.first,
            )
        case GetIssueCall(arguments=arguments):
            return await client.get_issue(arguments.issue_id)
        case CreateIssueCall(arguments=arguments):
            return await client.create_issue(
                team_id=arguments.team_id,
                title=arguments.title,
                description=arguments.description,
                project_id=arguments.project_id,
                state_id=None,
                label_ids=arguments.label_ids,
                priority=arguments.priority,
            )
        case ListTodosCall(arguments=arguments):
            return await list_workspace_todos(
                client,
                _workspace_context(workspace),
                team_key=os.environ.get("LINEAR_TEAM_KEY"),
                include_done=arguments.include_done,
            )
        case CreateTodoCall(arguments=arguments):
            return await create_workspace_todo(
                client,
                _workspace_context(workspace),
                WorkspaceTodoProposal(
                    title=arguments.title,
                    description=arguments.description,
                    priority=arguments.priority,
                ),
                team_key=os.environ.get("LINEAR_TEAM_KEY"),
                status=AGENT_PROPOSED_STATUS,
            )
        case CreateAttachmentCall(arguments=arguments):
            return await client.create_attachment(
                arguments.issue_id,
                arguments.url,
                arguments.title,
                subtitle=arguments.subtitle,
            )
        case _:
            return await client.find_issues_by_attachment_url(call.arguments.url)


def _workspace_context(workspace: str | None) -> WorkspaceContext:
    if not workspace:
        raise LinearError(_LINEAR_WORKSPACE_REQUIRED)
    name = workspace.replace("-", " ").replace("_", " ").title()
    return WorkspaceContext(folder=workspace, name=name)


def _json_result(value: object) -> dict[str, object]:
    return _text_result(json.dumps(value, indent=2, sort_keys=True))


def _text_result(text: str, *, is_error: bool = False) -> dict[str, object]:
    result: dict[str, object] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def _jsonrpc_result(request_id: object, result: dict[str, object]) -> web.Response:
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
        build_app(workspace=args.workspace),
        host=LOCAL_MCP_BIND_HOST,
        port=args.port,
    )


if __name__ == "__main__":
    main()
