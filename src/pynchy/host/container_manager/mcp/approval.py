"""MCP proxy approval contract."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class McpApprovalRequest:
    """Stable MCP capability and request evidence shown to the operator."""

    group_folder: str
    tool_name: str
    request_data: dict[str, Any]
    request_id: str
    capability_id: str
    reason: str


ApprovalRequestFn = Callable[[McpApprovalRequest], Awaitable[None]]
