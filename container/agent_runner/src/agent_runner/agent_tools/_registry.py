"""Tool registry for the agent MCP server."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from mcp.types import CallToolResult, TextContent, Tool


@dataclass
class ToolEntry:
    """A registered tool with its definition and handler."""

    definition: Callable[[], Tool | None]
    handler: Callable[..., Awaitable[list[TextContent] | CallToolResult]]


_TOOLS: dict[str, ToolEntry] = {}


def register(name: str, entry: ToolEntry) -> None:
    """Register a tool by name."""
    _TOOLS[name] = entry


def all_tools() -> list[Tool]:
    """Return all visible tool definitions (None = hidden for this context)."""
    return [t for e in _TOOLS.values() if (t := e.definition()) is not None]


def get_handler(name: str) -> Callable[..., Awaitable[list[TextContent] | CallToolResult]] | None:
    """Look up the handler for a tool name."""
    entry = _TOOLS.get(name)
    return entry.handler if entry else None
