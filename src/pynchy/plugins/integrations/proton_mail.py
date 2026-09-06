"""Proton Mail MCP server backed by local Bridge IMAP and SMTP."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable
from email.utils import parseaddr
from typing import Annotated, Literal, cast

import pluggy
from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from pynchy.logger import logger
from pynchy.plugins.api import McpServerConfig, McpServerSpec
from pynchy.plugins.integrations.proton_bridge import (
    ProtonMailClient,
    ProtonMailDelivery,
    ProtonMailError,
    create_proton_mail_client,
)
from pynchy.workspace.api import ServiceTrustConfig

hookimpl = pluggy.HookimplMarker("pynchy")

DEFAULT_PORT = 8475
LOCAL_MCP_BIND_HOST = "localhost"
_MAX_LIST_LIMIT = 200

type ProtonMailClientFactory = Callable[[], ProtonMailClient]
_MAIL_CLIENT_FACTORY_KEY: web.AppKey[ProtonMailClientFactory] = web.AppKey(
    "proton_mail_client_factory"
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ListMailboxesArguments(_StrictModel):
    """Arguments for mailbox listing."""


class _ListMailArguments(_StrictModel):
    """Arguments for message-envelope listing."""

    mailbox: str = "INBOX"
    limit: int = Field(default=20, ge=1, le=_MAX_LIST_LIMIT)
    offset: int = Field(default=0, ge=0)
    unread: bool = False

    @field_validator("mailbox")
    @classmethod
    def _validate_mailbox(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\r" in normalized or "\n" in normalized:
            raise ValueError("mailbox must be a single non-empty line")
        return normalized


class _ReadMailArguments(_StrictModel):
    """Arguments for fetching one message without altering its seen state."""

    mailbox: str = "INBOX"
    message_id: str = Field(min_length=1)
    headers: bool = False

    @field_validator("mailbox", "message_id")
    @classmethod
    def _validate_single_line(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\r" in normalized or "\n" in normalized:
            raise ValueError("value must be a single non-empty line")
        return normalized


class _SendMailArguments(_StrictModel):
    """Arguments for one plain-text SMTP submission through Proton Bridge."""

    to: list[str] = Field(min_length=1, max_length=100)
    subject: str = Field(max_length=998)
    body: str

    @field_validator("to")
    @classmethod
    def _validate_recipients(cls, value: list[str]) -> list[str]:
        return [_mail_address(recipient) for recipient in value]

    @field_validator("subject")
    @classmethod
    def _validate_subject(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("subject must be a single line")
        return value


class _DeleteMailArguments(_StrictModel):
    """Arguments for permanently removing one message by Message-ID."""

    mailbox: str = "INBOX"
    message_id: str = Field(min_length=1)

    @field_validator("mailbox", "message_id")
    @classmethod
    def _validate_single_line(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\r" in normalized or "\n" in normalized:
            raise ValueError("value must be a single non-empty line")
        return normalized


class _ListMailboxesCall(_StrictModel):
    name: Literal["proton_list_mailboxes"]
    arguments: _ListMailboxesArguments = Field(default_factory=_ListMailboxesArguments)


class _ListMailCall(_StrictModel):
    name: Literal["proton_list_mail"]
    arguments: _ListMailArguments = Field(default_factory=_ListMailArguments)


class _ReadMailCall(_StrictModel):
    name: Literal["proton_read_mail"]
    arguments: _ReadMailArguments


class _SendMailCall(_StrictModel):
    name: Literal["proton_send_mail"]
    arguments: _SendMailArguments


class _DeleteMailCall(_StrictModel):
    name: Literal["proton_delete_mail"]
    arguments: _DeleteMailArguments


class _ToolCallEnvelope(_StrictModel):
    name: str
    arguments: object = Field(default_factory=dict)
    metadata: object = Field(default=None, alias="_meta", exclude=True)


type ProtonToolCall = Annotated[
    _ListMailboxesCall | _ListMailCall | _ReadMailCall | _SendMailCall | _DeleteMailCall,
    Field(discriminator="name"),
]
_TOOL_CALL_ADAPTER: TypeAdapter[ProtonToolCall] = TypeAdapter(ProtonToolCall)


class _McpRequest(_StrictModel):
    """The JSON-RPC fields this server accepts at its HTTP boundary."""

    jsonrpc: Literal["2.0"]  # noqa: V107
    id: int | str | None = None
    method: str
    params: object = None


class ProtonMailMcpPlugin:  # noqa: V102
    """Register a host-side MCP server for Proton Mail through local Bridge."""

    @hookimpl
    def pynchy_mcp_server_spec(self) -> tuple[McpServerSpec, ...]:
        # Mail delivery and deletion must remain usable operationally; declare their
        # external, irreversible effects so Pynchy's normal approval gate protects them.
        return (
            McpServerSpec(
                name="proton-mail",
                config=McpServerConfig(
                    type="script",
                    command=sys.executable,
                    args=[
                        "-m",
                        "pynchy.plugins.integrations.proton_mail",
                        "--port",
                        "{port}",
                    ],
                    port=DEFAULT_PORT,
                    transport="streamable_http",
                    idle_timeout=600,
                    # Concurrent host imports can consume nearly five seconds before HTTP binds.
                    startup_timeout_seconds=10,
                ),
                trust=ServiceTrustConfig(),
            ),
        )


def build_app(
    mail_client_factory: ProtonMailClientFactory = create_proton_mail_client,
) -> web.Application:
    """Build the local MCP HTTP application."""
    app = web.Application()
    app[_MAIL_CLIENT_FACTORY_KEY] = mail_client_factory
    app.router.add_get("/", _handle_health)
    app.router.add_post("/mcp", _handle_mcp)
    return app


async def _handle_health(  # noqa: RUF029 - aiohttp route handlers are async.
    _request: web.Request,
) -> web.Response:
    return web.json_response({"status": "ok", "service": "pynchy-proton-mail"})


async def _handle_mcp(request: web.Request) -> web.StreamResponse:
    try:
        payload = _McpRequest.model_validate(await request.json())
    except ValidationError as exc:
        return _jsonrpc_error(None, -32600, f"Invalid JSON-RPC request: {exc}")

    try:
        return await _dispatch_mcp_request(payload, request.app)
    except Exception as exc:  # noqa: BLE001 - MCP boundary reports tool failures to callers.
        logger.exception("Proton Mail MCP request failed", method=payload.method)
        return _jsonrpc_result(
            payload.id,
            _text_result(f"Proton Mail tool failed: {exc}", is_error=True),
        )


async def _dispatch_mcp_request(payload: _McpRequest, app: web.Application) -> web.StreamResponse:
    if payload.method == "initialize":
        return _jsonrpc_result(payload.id, _initialize_result())
    if payload.method == "notifications/initialized":
        return web.Response(status=202)
    if payload.method == "tools/list":
        return _jsonrpc_result(payload.id, {"tools": _tool_specs()})
    if payload.method == "tools/call":
        factory = app[_MAIL_CLIENT_FACTORY_KEY]
        return _jsonrpc_result(payload.id, await _call_tool(payload.params, factory))
    return _jsonrpc_error(payload.id, -32601, f"Unknown MCP method: {payload.method}")


def _initialize_result() -> dict[str, object]:
    return {
        "protocolVersion": "2025-06-18",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "pynchy-proton-mail", "version": "0.3.0"},
    }


def _tool_specs() -> list[dict[str, object]]:
    return [
        {
            "name": "proton_list_mailboxes",
            "description": "List Proton Mail mailboxes available through the host Proton Bridge.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "proton_list_mail",
            "description": (
                "List Proton Mail messages through direct Bridge IMAP. "
                "Use a returned Message-ID with proton_read_mail; do not persist IMAP UIDs."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mailbox": {
                        "type": "string",
                        "description": "Mailbox identifier returned by proton_list_mailboxes.",
                        "default": "INBOX",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of messages to return (1-200).",
                        "default": 20,
                        "minimum": 1,
                        "maximum": _MAX_LIST_LIMIT,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Number of messages to skip.",
                        "default": 0,
                        "minimum": 0,
                    },
                    "unread": {
                        "type": "boolean",
                        "description": "Return only unread messages.",
                        "default": False,
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "proton_read_mail",
            "description": (
                "Read a Proton Mail message by Message-ID without changing its read/unread state."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Message-ID returned by proton_list_mail.",
                    },
                    "mailbox": {
                        "type": "string",
                        "description": "Mailbox identifier returned by proton_list_mailboxes.",
                        "default": "INBOX",
                    },
                    "headers": {
                        "type": "boolean",
                        "description": "Include all message headers.",
                        "default": False,
                    },
                },
                "required": ["message_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "proton_send_mail",
            "description": (
                "Send a plain-text Proton Mail message through the host Proton Bridge. "
                "This external, irreversible action requires Pynchy approval."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "to": {
                        "type": "array",
                        "items": {"type": "string", "format": "email"},
                        "description": "Recipient email addresses.",
                        "minItems": 1,
                        "maxItems": 100,
                    },
                    "subject": {
                        "type": "string",
                        "description": "Single-line message subject.",
                        "maxLength": 998,
                    },
                    "body": {
                        "type": "string",
                        "description": "Plain-text message body.",
                    },
                },
                "required": ["to", "subject", "body"],
                "additionalProperties": False,
            },
        },
        {
            "name": "proton_delete_mail",
            "description": (
                "Permanently delete one Proton Mail message by Message-ID from a mailbox. "
                "This irreversible action requires Pynchy approval."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Message-ID returned by proton_list_mail.",
                    },
                    "mailbox": {
                        "type": "string",
                        "description": "Mailbox identifier returned by proton_list_mailboxes.",
                        "default": "INBOX",
                    },
                },
                "required": ["message_id"],
                "additionalProperties": False,
            },
        },
    ]


async def _call_tool(
    params: object,
    mail_client_factory: ProtonMailClientFactory,
) -> dict[str, object]:
    tool_call = _parse_tool_call(params)
    client = mail_client_factory()

    result: object
    if isinstance(tool_call, _ListMailboxesCall):
        result = await asyncio.to_thread(client.list_mailboxes)
    elif isinstance(tool_call, _ListMailCall):
        list_arguments = tool_call.arguments
        result = await asyncio.to_thread(
            client.list_mail,
            mailbox=list_arguments.mailbox,
            limit=list_arguments.limit,
            offset=list_arguments.offset,
            unread=list_arguments.unread,
        )
    elif isinstance(tool_call, _ReadMailCall):
        read_arguments = tool_call.arguments
        result = await asyncio.to_thread(
            client.read_mail,
            mailbox=read_arguments.mailbox,
            message_id=read_arguments.message_id,
            include_headers=read_arguments.headers,
        )
    elif isinstance(tool_call, _SendMailCall):
        send_arguments = tool_call.arguments
        result = await asyncio.to_thread(
            client.send_mail,
            recipients=send_arguments.to,
            subject=send_arguments.subject,
            body=send_arguments.body,
        )
    else:
        delete_arguments = tool_call.arguments
        await asyncio.to_thread(
            client.delete_mail,
            mailbox=delete_arguments.mailbox,
            message_id=delete_arguments.message_id,
        )
        result = ProtonMailDelivery(message_id=delete_arguments.message_id)
    return _json_result(cast("BaseModel", result).model_dump(mode="json"))


def _parse_tool_call(params: object) -> ProtonToolCall:
    try:
        envelope = _ToolCallEnvelope.model_validate(params)
        return _TOOL_CALL_ADAPTER.validate_python(
            {"name": envelope.name, "arguments": envelope.arguments}
        )
    except ValidationError as exc:
        raise ProtonMailError(f"Invalid Proton Mail tool arguments: {exc}") from exc


def _mail_address(value: str) -> str:
    """Accept one bare SMTP mailbox and reject header injection or display names."""
    normalized = value.strip()
    if not normalized or "\r" in normalized or "\n" in normalized:
        raise ValueError("recipient must be a single non-empty line")
    _display_name, address = parseaddr(normalized)
    if address != normalized or address.count("@") != 1:
        raise ValueError("recipient must be a bare email address")
    local_part, domain = address.rsplit("@", maxsplit=1)
    if not local_part or not domain or any(character.isspace() for character in address):
        raise ValueError("recipient must be a valid email address")
    return address


def _json_result(value: object) -> dict[str, object]:
    return _text_result(json.dumps(value, indent=2, sort_keys=True))


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
    parser = argparse.ArgumentParser(description="Run the Pynchy Proton Mail MCP server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", DEFAULT_PORT)))
    args = parser.parse_args(argv)
    web.run_app(build_app(), host=LOCAL_MCP_BIND_HOST, port=args.port)


if __name__ == "__main__":
    main()
