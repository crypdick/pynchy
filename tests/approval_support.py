"""Shared encrypted approval fixtures."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pynchy.host.container_manager.security.approval import (
    configure_approval_state_root,
    create_pending_approval,
    read_pending_approval,
)
from pynchy.workspace.api import APPROVAL_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from pathlib import Path


def write_encrypted_pending_approval(
    root: Path,
    *,
    request_id: str,
    tool_name: str,
    source_group: str,
    approval_chat_jid: str,
    request_data: dict[str, Any],
    handler_type: str = "service",
    expires_after_seconds: int = APPROVAL_TIMEOUT_SECONDS,
    approval_scope: str = "exact_request",
    capability_id: str | None = None,
    origin_conversation_id: str | None = None,
    action_payload: dict[str, Any] | None = None,
    timestamp: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Create production-shaped encrypted pending state for approval tests."""
    configure_approval_state_root(root)
    create_pending_approval(
        request_id=request_id,
        tool_name=tool_name,
        source_group=source_group,
        approval_chat_jid=approval_chat_jid,
        request_data=request_data,
        handler_type=handler_type,
        expires_after_seconds=expires_after_seconds,
        approval_scope=approval_scope,
        capability_id=capability_id,
        origin_conversation_id=origin_conversation_id,
        action_payload=action_payload,
    )
    path = root / source_group / "pending_approvals" / f"{request_id}.json"
    if timestamp is not None:
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["timestamp"] = timestamp
        path.write_text(json.dumps(raw), encoding="utf-8")
    return path, read_pending_approval(path)
