"""MCP startup outcome types shared by lifecycle callers."""

from __future__ import annotations

from dataclasses import dataclass

from pynchy.types import McpStartupFailure  # noqa: TC001, RUF100 - public import contract.


@dataclass(frozen=True)
class McpWorkspaceStartup:
    """Readiness outcome for one workspace's MCP servers."""

    ready_instance_ids: tuple[str, ...]
    failures: tuple[McpStartupFailure, ...]
