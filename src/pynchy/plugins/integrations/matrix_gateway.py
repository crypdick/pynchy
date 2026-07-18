"""Approval-gated Matrix MCP server backed by the host-only communications gateway."""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Callable
from typing import Annotated, Literal, cast

import pluggy
from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from pynchy.logger import logger
from pynchy.plugins.integrations.matrix_gateway_client import (
    MatrixGatewayClient,
    MatrixGatewayError,
    create_matrix_gateway_client,
    json_result,
)

hookimpl = pluggy.HookimplMarker("pynchy")

DEFAULT_PORT = 8476
LOCAL_MCP_BIND_HOST = "localhost"
_MAX_LIST_LIMIT = 250

type MatrixGatewayClientFactory = Callable[[], MatrixGatewayClient]
_GATEWAY_CLIENT_FACTORY_KEY: web.AppKey[MatrixGatewayClientFactory] = web.AppKey(
    "matrix_gateway_client_factory"
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ListChatsArguments(_StrictModel):
    """Arguments for Matrix chat listing."""


class _ListMessagesArguments(_StrictModel):
    """Arguments for Matrix message listing."""

    room_id: str = Field(min_length=1)
    limit: int = Field(default=50, ge=1, le=_MAX_LIST_LIMIT)

    @field_validator("room_id")
    @classmethod
    def _validate_room_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\r" in normalized or "\n" in normalized:
            raise ValueError("room_id must be a single non-empty line")
        return normalized


class _SendMessageArguments(_StrictModel):
    """Arguments for one approval-gated external Matrix message."""

    room_id: str = Field(min_length=1)
    body: str = Field(min_length=1)

    @field_validator("room_id")
    @classmethod
    def _validate_room_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\r" in normalized or "\n" in normalized:
            raise ValueError("room_id must be a single non-empty line")
        return normalized

    @field_validator("body")
    @classmethod
    def _validate_body(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("body must not be empty")
        return value


class _ListChatsCall(_StrictModel):
    name: Literal["matrix_list_chats"]
    arguments: _ListChatsArguments = Field(default_factory=_ListChatsArguments)


class _ListMessagesCall(_StrictModel):
    name: Literal["matrix_list_messages"]
    arguments: _ListMessagesArguments


class _SendMessageCall(_StrictModel):
    name: Literal["matrix_send_message"]
    arguments: _SendMessageArguments


type MatrixToolCall = Annotated[
    _ListChatsCall | _ListMessagesCall | _SendMessageCall,
    Field(discriminator="name"),
]
_TOOL_CALL_ADAPTER: TypeAdapter[MatrixToolCall] = TypeAdapter(MatrixToolCall)


class _McpRequest(_StrictModel):
    """The JSON-RPC fields this server accepts at its HTTP boundary."""

    jsonrpc: Literal["2.0"]
    id: int | str | None = None
    method: str
    params: object = None


class MatrixGatewayMcpPlugin:
    """Register the host-only Matrix communications gateway with approval-gated sends."""

    @hookimpl
    def pynchy_mcp_server_spec(self) -> dict[str, object]:
        return {
            "name": "matrix-gateway",
            "type": "script",
            "command": "uv",
            "args": [
                "run",
                "python",
                "-m",
                "pynchy.plugins.integrations.matrix_gateway",
                "--port",
                "{port}",
            ],
            "port": DEFAULT_PORT,
            "transport": "streamable_http",
            "idle_timeout": 600,
            "trust": {
                "public_source": True,
                "secret_data": True,
                "public_sink": True,
                "dangerous_writes": True,
            },
        }


def build_app(
    gateway_client_factory: MatrixGatewayClientFactory = create_matrix_gateway_client,
) -> web.Application:
    """Build the loopback-only Matrix gateway MCP application."""
    app = web.Application()
    app[_GATEWAY_CLIENT_FACTORY_KEY] = gateway_client_factory
    app.router.add_get("/", _handle_health)
    app.router.add_post("/mcp", _handle_mcp)
    return app


async def _handle_health(  # noqa: RUF029, RUF100 - aiohttp route handlers are async.
    _request: web.Request,
) -> web.Response:
    return web.json_response({"status": "ok", "service": "pynchy-matrix-gateway"})


async def _handle_mcp(request: web.Request) -> web.StreamResponse:
    try:
        payload = _McpRequest.model_validate(await request.json())
    except ValidationError as exc:
        return _jsonrpc_error(None, -32600, f"Invalid JSON-RPC request: {exc}")

    try:
        return await _dispatch_mcp_request(payload, request.app)
    except MatrixGatewayError as exc:
        return _jsonrpc_result(payload.id, _text_result(str(exc), is_error=True))
    except Exception:  # noqa: BLE001, RUF100 - MCP boundary reports tool failures to callers.
        logger.exception("Matrix gateway MCP request failed", method=payload.method)
        return _jsonrpc_result(
            payload.id,
            _text_result("Matrix gateway tool failed", is_error=True),
        )


async def _dispatch_mcp_request(payload: _McpRequest, app: web.Application) -> web.StreamResponse:
    if payload.method == "initialize":
        return _jsonrpc_result(payload.id, _initialize_result())
    if payload.method == "notifications/initialized":
        return web.Response(status=202)
    if payload.method == "tools/list":
        return _jsonrpc_result(payload.id, {"tools": _tool_specs()})
    if payload.method == "tools/call":
        factory = app[_GATEWAY_CLIENT_FACTORY_KEY]
        return _jsonrpc_result(payload.id, await _call_tool(payload.params, factory))
    return _jsonrpc_error(payload.id, -32601, f"Unknown MCP method: {payload.method}")


def _initialize_result() -> dict[str, object]:
    return {
        "protocolVersion": "2025-06-18",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "pynchy-matrix-gateway", "version": "0.1.0"},
    }


def _tool_specs() -> list[dict[str, object]]:
    return [
        {
            "name": "matrix_list_chats",
            "description": "List chats available through the owner's private Matrix gateway.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "matrix_list_messages",
            "description": "Read recent text messages from one Matrix chat without changing it.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "room_id": {"type": "string", "description": "Room ID from matrix_list_chats."},
                    "limit": {
                        "type": "integer",
                        "description": "Number of messages to return (1-250).",
                        "default": 50,
                        "minimum": 1,
                        "maximum": _MAX_LIST_LIMIT,
                    },
                },
                "required": ["room_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "matrix_send_message",
            "description": (
                "Send a plain-text message as the Matrix gateway owner. The recipient sees the "
                "owner's bridged account, not Pynchy. This external action requires approval."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "room_id": {"type": "string", "description": "Room ID from matrix_list_chats."},
                    "body": {"type": "string", "description": "Final approved plain-text message."},
                },
                "required": ["room_id", "body"],
                "additionalProperties": False,
            },
        },
    ]


async def _call_tool(
    params: object,
    gateway_client_factory: MatrixGatewayClientFactory,
) -> dict[str, object]:
    tool_call = _parse_tool_call(params)
    client = gateway_client_factory()
    result: object
    if isinstance(tool_call, _ListChatsCall):
        result = await asyncio.to_thread(client.list_chats)
    elif isinstance(tool_call, _ListMessagesCall):
        message_arguments = tool_call.arguments
        result = await asyncio.to_thread(
            client.list_messages,
            room_id=message_arguments.room_id,
            limit=message_arguments.limit,
        )
    else:
        send_arguments = tool_call.arguments
        result = await asyncio.to_thread(
            client.send_message,
            room_id=send_arguments.room_id,
            body=send_arguments.body,
        )
    return _text_result(json_result(cast("BaseModel | list[BaseModel]", result)))


def _parse_tool_call(params: object) -> MatrixToolCall:
    try:
        return _TOOL_CALL_ADAPTER.validate_python(params)
    except ValidationError as exc:
        raise MatrixGatewayError(f"Invalid Matrix gateway tool arguments: {exc}") from exc


def _text_result(text: str, *, is_error: bool = False) -> dict[str, object]:
    result: dict[str, object] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def _jsonrpc_result(request_id: int | str | None, result: dict[str, object]) -> web.Response:
    return web.json_response({"jsonrpc": "2.0", "id": request_id, "result": result})


def _jsonrpc_error(request_id: int | str | None, code: int, message: str) -> web.Response:
    return web.json_response(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Pynchy Matrix gateway MCP server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", DEFAULT_PORT)))
    args = parser.parse_args(argv)
    web.run_app(build_app(), host=LOCAL_MCP_BIND_HOST, port=args.port)


if __name__ == "__main__":
    main()
