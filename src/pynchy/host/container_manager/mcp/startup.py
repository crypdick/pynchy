"""MCP startup outcome types shared by lifecycle callers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class McpStartupFailure:
    """A newly observed MCP startup failure that callers can surface to users."""

    instance_id: str
    server_name: str
    reason: str


@dataclass(frozen=True)
class McpWorkspaceStartup:
    """Readiness outcome for one workspace's MCP servers."""

    ready_instance_ids: tuple[str, ...]
    failures: tuple[McpStartupFailure, ...]
