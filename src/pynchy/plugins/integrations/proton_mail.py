"""Read-only Proton Mail MCP server backed by the host's pm-cli installation."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from typing import Any, cast

import pluggy
from aiohttp import web

from pynchy.logger import logger

hookimpl = pluggy.HookimplMarker("pynchy")

DEFAULT_PORT = 8475
LOCAL_MCP_BIND_HOST = "localhost"
_PM_CLI_ENV = "PYNCHY_PROTON_PM_CLI"
_MAX_LIST_LIMIT = 200
_READ_STATE_SCAN_LIMIT = 500
_UID_PATTERN = re.compile(r"^[1-9][0-9]*$")


class ProtonMailError(RuntimeError):
    """Raised when the local Proton Mail integration cannot complete an operation."""


class ProtonMailMcpPlugin:
    """Register a host-side, read-only MCP server for Proton Mail."""

    @hookimpl
    def pynchy_mcp_server_spec(self) -> dict[str, Any]:
        return {
            "name": "proton-mail",
            "type": "script",
            "command": "uv",
            "args": [
                "run",
                "python",
                "-m",
                "pynchy.plugins.integrations.proton_mail",
                "--port",
                "{port}",
            ],
            "port": DEFAULT_PORT,
            "transport": "streamable_http",
            "idle_timeout": 600,
            "trust": {
                "public_source": False,
                "secret_data": True,
                "public_sink": False,
                "dangerous_writes": False,
            },
        }


def build_app() -> web.Application:
    """Build the local MCP HTTP application."""
    app = web.Application()
    app.router.add_get("/", _handle_health)
    app.router.add_post("/mcp", _handle_mcp)
    return app


async def _handle_health(  # noqa: RUF029, RUF100 - aiohttp route handlers are async.
    _request: web.Request,
) -> web.Response:
    return web.json_response({"status": "ok", "service": "pynchy-proton-mail"})


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
            return _jsonrpc_result(request_id, {"tools": _tool_specs()})
        if method == "tools/call":
            return _jsonrpc_result(request_id, await _call_tool(params))
        return _jsonrpc_error(request_id, -32601, f"Unknown MCP method: {method}")
    except Exception as exc:  # noqa: BLE001, RUF100 - MCP boundary reports tool failures to callers.
        logger.exception("Proton Mail MCP request failed", method=method)
        return _jsonrpc_result(
            request_id,
            _text_result(f"Proton Mail tool failed: {exc}", is_error=True),
        )


def _initialize_result() -> dict[str, Any]:
    return {
        "protocolVersion": "2025-06-18",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "pynchy-proton-mail", "version": "0.1.0"},
    }


def _tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "proton_list_mailboxes",
            "description": "List Proton Mail mailboxes available through the host Proton Bridge.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "proton_list_mail",
            "description": "List Proton Mail messages. Use the returned UID with proton_read_mail.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mailbox": {
                        "type": "string",
                        "description": "Mailbox name.",
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
            },
        },
        {
            "name": "proton_read_mail",
            "description": (
                "Read a Proton Mail message by UID while preserving its original read/unread state."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "uid": {
                        "type": "string",
                        "description": "UID from proton_list_mail, without the uid: prefix.",
                    },
                    "mailbox": {
                        "type": "string",
                        "description": "Mailbox containing the message.",
                        "default": "INBOX",
                    },
                    "headers": {
                        "type": "boolean",
                        "description": "Include all message headers.",
                        "default": False,
                    },
                },
                "required": ["uid"],
            },
        },
    ]


async def _call_tool(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _text_result("Tool arguments must be an object", is_error=True)

    if name == "proton_list_mailboxes":
        return _json_result(await _proton_json("mailbox", "list", "--json"))
    if name == "proton_list_mail":
        return _json_result(await _list_mail(arguments))
    if name == "proton_read_mail":
        return _json_result(await _read_mail(arguments))
    return _text_result(f"Unknown Proton Mail tool: {name}", is_error=True)


async def _list_mail(arguments: dict[str, Any]) -> object:
    mailbox = _mailbox(arguments)
    limit = _bounded_int(arguments, "limit", default=20, minimum=1, maximum=_MAX_LIST_LIMIT)
    offset = _bounded_int(arguments, "offset", default=0, minimum=0)
    args = ["mail", "list", "--mailbox", mailbox, "--limit", str(limit), "--offset", str(offset)]
    if arguments.get("unread") is True:
        args.append("--unread")
    args.append("--json")
    return await _proton_json(*args)


async def _read_mail(arguments: dict[str, Any]) -> object:
    mailbox = _mailbox(arguments)
    uid = _uid(arguments)
    listing = await _proton_json(
        "mail",
        "list",
        "--mailbox",
        mailbox,
        "--limit",
        str(_READ_STATE_SCAN_LIMIT),
        "--json",
    )
    seen = _message_seen(listing, uid)

    read_args = ["mail", "read", "--mailbox", mailbox, f"uid:{uid}", "--json"]
    if arguments.get("headers") is True:
        read_args.append("--headers")

    try:
        result = await _proton_json(*read_args)
    finally:
        await _run_proton_command(
            "mail",
            "flag",
            "--mailbox",
            mailbox,
            f"uid:{uid}",
            "--read" if seen else "--unread",
        )
    return result


def _message_seen(listing: object, uid: str) -> bool:
    if not isinstance(listing, dict):
        raise ProtonMailError("Could not determine the message's read state")
    messages = listing.get("messages")
    if not isinstance(messages, list):
        raise ProtonMailError("Could not determine the message's read state")
    for message in messages:
        if not isinstance(message, dict) or str(message.get("uid")) != uid:
            continue
        seen = message.get("seen")
        if isinstance(seen, bool):
            return seen
        raise ProtonMailError("The message did not report its read state")
    raise ProtonMailError(
        "Message UID was not found in the recent mailbox listing; "
        "list the mailbox before reading it"
    )


def _mailbox(arguments: dict[str, Any]) -> str:
    value = arguments.get("mailbox", "INBOX")
    if not isinstance(value, str) or not value.strip() or value.startswith("-"):
        raise ProtonMailError("mailbox must be a non-empty mailbox name")
    return value.strip()


def _uid(arguments: dict[str, Any]) -> str:
    value = arguments.get("uid")
    if not isinstance(value, str) or not _UID_PATTERN.fullmatch(value):
        raise ProtonMailError("uid must be a positive numeric UID without the uid: prefix")
    return value


def _bounded_int(
    arguments: dict[str, Any],
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int | None = None,
) -> int:
    value = arguments.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtonMailError(f"{key} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        boundary = (
            f"between {minimum} and {maximum}" if maximum is not None else f"at least {minimum}"
        )
        raise ProtonMailError(f"{key} must be {boundary}")
    return value


async def _proton_json(*args: str) -> object:
    output = await _run_proton_command(*args)
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise ProtonMailError("pm-cli did not return valid JSON") from exc


async def _run_proton_command(*args: str) -> str:
    executable = os.environ.get(_PM_CLI_ENV)
    if not executable:
        raise ProtonMailError(f"{_PM_CLI_ENV} is not configured on the host")

    process = await asyncio.create_subprocess_exec(
        executable,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise ProtonMailError("pm-cli timed out after 60 seconds") from exc

    output = stdout.decode(errors="replace")
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip() or output.strip()
        raise ProtonMailError(detail or f"pm-cli exited with status {process.returncode}")
    return output


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
    parser = argparse.ArgumentParser(description="Run the Pynchy Proton Mail MCP server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", DEFAULT_PORT)))
    args = parser.parse_args(argv)
    web.run_app(build_app(), host=LOCAL_MCP_BIND_HOST, port=args.port)


if __name__ == "__main__":
    main()
