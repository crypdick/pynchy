"""Shared pending-approval fixtures."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from pynchy.host.container_manager.security.approval import create_pending_approval

if TYPE_CHECKING:
    from pathlib import Path


def write_pending_approval(
    ipc_dir: Path,
    group: str,
    request_id: str,
    tool_name: str,
    request_data: dict[str, Any],
    handler_type: str = "service",
) -> Path:
    """Create a valid encrypted approval record for handler tests."""
    executable_request = {
        "type": f"service:{tool_name}" if handler_type == "service" else tool_name,
        "request_id": request_id,
        **request_data,
    }
    with patch(
        "pynchy.host.container_manager.security.approval._approval_root",
        ipc_dir.parent / "approvals",
    ):
        create_pending_approval(
            request_id=request_id,
            tool_name=tool_name,
            source_group=group,
            approval_chat_jid="j@g.us",
            request_data=executable_request,
            handler_type=handler_type,
        )
    return ipc_dir.parent / "approvals" / group / "pending_approvals" / f"{request_id}.json"
