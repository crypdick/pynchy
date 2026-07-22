"""Tool registry for the agent MCP server."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from mcp.types import CallToolResult, TextContent, Tool

StructuredToolResult = tuple[list[TextContent], dict[str, Any]]
HandlerResult = list[TextContent] | CallToolResult | StructuredToolResult
Handler = Callable[..., Awaitable[HandlerResult]]


@dataclass
class ToolEntry:
    """A registered tool with its definition and handler."""

    definition: Callable[[], Tool | None]
    handler: Handler


_TOOLS: dict[str, ToolEntry] = {}


def register(name: str, entry: ToolEntry) -> None:
    """Register a tool by name."""
    _TOOLS[name] = entry


def tool(
    name: str,
    description: str,
    input_schema: dict[str, Any] | None = None,
    *,
    visible: Callable[[], bool] | None = None,
) -> Callable[[Handler], Handler]:
    """Register a local-logic tool by decorating its handler.

    Fuses the :class:`Tool` definition and registration onto the handler,
    replacing the ``_xxx_definition()`` + ``register(ToolEntry(...))`` ritual.

    Pass *visible* (a no-arg predicate) for tools that should be hidden in some
    contexts — the definition returns ``None`` when it evaluates falsy, which
    :func:`all_tools` filters out.

    Use :func:`register_ipc_tool` instead for pure IPC-proxy tools, and the
    explicit :func:`register` for tools whose definition needs richer runtime
    logic (dynamic schema/description).
    """
    schema = input_schema if input_schema is not None else {"type": "object", "properties": {}}

    def deco(handler: Handler) -> Handler:
        def definition() -> Tool | None:
            if visible is not None and not visible():
                return None
            return Tool(name=name, description=description, inputSchema=schema)

        register(name, ToolEntry(definition=definition, handler=handler))
        return handler

    return deco


def register_ipc_tool(
    name: str,
    description: str,
    input_schema: dict[str, Any],
) -> None:
    """Register a tool that forwards arguments to an IPC service request.

    Generates both the Tool definition and an async handler that calls
    ``ipc_service_request(name, arguments)``, applying any ``default``
    values declared in the input schema for fields the caller omits.

    Use this for tools that are pure IPC proxies — no local validation
    or custom logic, just forwarding to the host.
    """
    from ._ipc_request import (  # noqa: PLC0415, RUF100 - only IPC-proxy tools need watchdog-backed IPC setup.
        ipc_service_request,
    )

    # Pre-compute defaults from schema so the handler closure is fast.
    defaults: dict[str, Any] = {}
    for prop_name, prop_def in input_schema.get("properties", {}).items():
        if "default" in prop_def:
            defaults[prop_name] = prop_def["default"]

    async def handler(arguments: dict[str, Any]) -> list[TextContent]:
        request = {**defaults, **arguments}
        return await ipc_service_request(name, request)

    register(
        name,
        ToolEntry(
            definition=lambda: Tool(name=name, description=description, inputSchema=input_schema),
            handler=handler,
        ),
    )


def all_tools() -> list[Tool]:
    """Return all visible tool definitions (None = hidden for this context)."""
    return [t for e in _TOOLS.values() if (t := e.definition()) is not None]


def tool_error(msg: str) -> CallToolResult:
    """Return an MCP error result with a text message."""
    return CallToolResult(
        content=[TextContent(type="text", text=msg)],
        isError=True,
    )


def get_handler(name: str) -> Handler | None:
    """Look up the handler for a tool name."""
    entry = _TOOLS.get(name)
    return entry.handler if entry else None
