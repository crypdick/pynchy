"""Strict durable-state parsing for human approval decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pynchy.conversation.api import ConversationId
from pynchy.host.container_manager.ipc.approval_replay import (
    ApprovalDecisionContext,
)
from pynchy.host.container_manager.ipc.handlers_service import _get_action_catalog
from pynchy.host.container_manager.security.gate import (  # noqa: TC001 - beartype resolves replay-gate annotations.
    SecurityGate,
)
from pynchy.workspace.api import APPROVAL_TIMEOUT_SECONDS


@dataclass(frozen=True)
class ApprovalDecision:
    """Strict host-written human decision."""

    request_id: str
    approved: bool
    decided_by: str
    decided_at: str
    guarded_action_id: str | None = None
    request_payload_hash: str | None = None
    source_group: str | None = None
    approval_scope: str | None = None

    @classmethod
    def parse(cls, value: object) -> ApprovalDecision:
        """Parse a decision without truthy or identity fallbacks."""
        if not isinstance(value, dict):
            raise TypeError("approval decision must be a JSON object")
        required_fields = {
            "request_id",
            "guarded_action_id",
            "request_payload_hash",
            "source_group",
            "approved",
            "decided_by",
            "decided_at",
        }
        if set(value) not in (required_fields, {*required_fields, "approval_scope"}):
            raise ValueError("approval decision has missing or unexpected fields")
        request_id = value["request_id"]
        approved = value["approved"]
        decided_by = value["decided_by"]
        decided_at = value["decided_at"]
        guarded_action_id = value["guarded_action_id"]
        request_payload_hash = value["request_payload_hash"]
        source_group = value["source_group"]
        approval_scope = value.get("approval_scope")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("approval decision request_id must be a non-empty string")
        if type(approved) is not bool:
            raise ValueError("approval decision approved must be a boolean")
        if not isinstance(decided_by, str) or not decided_by:
            raise ValueError("approval decision decided_by must be a non-empty string")
        if not isinstance(decided_at, str) or not decided_at:
            raise ValueError("approval decision decided_at must be a non-empty string")
        for field_name, field_value in (
            ("guarded_action_id", guarded_action_id),
            ("request_payload_hash", request_payload_hash),
            ("source_group", source_group),
        ):
            if not isinstance(field_value, str) or not field_value:
                raise ValueError(f"approval decision {field_name} must be a non-empty string")
        try:
            parsed_decided_at = datetime.fromisoformat(decided_at)
        except ValueError as exc:
            raise ValueError("approval decision decided_at must be an ISO timestamp") from exc
        if parsed_decided_at.tzinfo is None or parsed_decided_at.utcoffset() is None:
            raise ValueError("approval decision decided_at must include a timezone")
        if approval_scope not in {None, "once", "session", "forever"}:
            raise ValueError("approval decision approval_scope is invalid")
        if not approved and approval_scope is not None:
            raise ValueError("denied approval decision cannot have an approval_scope")
        return cls(
            request_id=request_id,
            approved=approved,
            decided_by=decided_by,
            decided_at=decided_at,
            guarded_action_id=guarded_action_id,
            request_payload_hash=request_payload_hash,
            source_group=source_group,
            approval_scope=approval_scope,
        )


@runtime_checkable
class ApprovalReplayGateResolver(Protocol):
    """Build the current approval gate for one already-bound source group."""

    def __call__(
        self,
        *,
        require_resolved: bool,
        request_corruption_tainted: bool,
        request_secret_tainted: bool,
    ) -> SecurityGate | None: ...


def build_approval_decision_context(
    pending: dict[str, Any],
    decision: ApprovalDecision,
    *,
    source_group: str,
    replay_gate: ApprovalReplayGateResolver,
) -> ApprovalDecisionContext:
    """Build replay context from validated host-owned durable state."""
    tool_name = _required_nonempty_string(pending, "tool_name")
    chat_jid = _required_nonempty_string(pending, "approval_chat_jid")
    request_data = pending.get("request_data")
    if not isinstance(request_data, dict):
        raise TypeError("pending approval request_data must be an object")
    handler_type = pending.get("handler_type")
    if handler_type not in {
        "service",
        "ipc",
        "mcp_proxy",
        "security_bash",
        "security_artifact",
    }:
        raise ValueError("pending approval handler_type is invalid")
    action = _get_action_catalog().action_for(tool_name) if handler_type == "service" else None
    capability_id = str(action.capability.id) if action is not None else None
    if decision.approval_scope in {"session", "forever"}:
        pending_capability = pending.get("capability_id")
        if (
            pending.get("allow_remember") is not True
            or not isinstance(pending_capability, str)
            or pending_capability != capability_id
        ):
            raise ValueError("pending approval does not support reusable approval")
    action_ids = (
        tuple(str(action_id) for action_id in action.capability.action_ids)
        if action is not None
        else ()
    )
    origin = pending.get("origin_conversation_id")
    if origin is not None and not isinstance(origin, str):
        raise ValueError("pending approval origin_conversation_id must be a string or null")
    origin_conversation_id = ConversationId(origin) if isinstance(origin, str) and origin else None
    gate = replay_gate(
        require_resolved=origin_conversation_id is not None,
        request_corruption_tainted=_persisted_taint(pending, "corruption_tainted"),
        request_secret_tainted=_persisted_secret_taint(pending),
    )
    action_payload = pending.get("action_payload")
    if action_payload is not None and not isinstance(action_payload, dict):
        raise TypeError("pending approval action_payload must be an object or null")
    raw_timeout = pending.get("expires_after_seconds", APPROVAL_TIMEOUT_SECONDS)
    approval_scope = decision.approval_scope or _legacy_approval_scope(pending, action)
    return ApprovalDecisionContext(
        request_id=decision.request_id,
        source_group=source_group,
        tool_name=tool_name,
        chat_jid=chat_jid,
        request_data=request_data,
        approved=decision.approved,
        approver=decision.decided_by,
        approved_at=decision.decided_at,
        handler_type=handler_type,
        action=action,
        gate=gate,
        capability_id=capability_id,
        action_ids=action_ids,
        origin_conversation_id=origin_conversation_id,
        action_payload=action_payload if isinstance(action_payload, dict) else None,
        action_payload_sha256=(
            pending.get("action_payload_sha256")
            if isinstance(pending.get("action_payload_sha256"), str)
            else None
        ),
        requested_at=(
            pending.get("timestamp") if isinstance(pending.get("timestamp"), str) else None
        ),
        expires_after_seconds=(
            raw_timeout if isinstance(raw_timeout, int) and raw_timeout > 0 else 0
        ),
        approval_scope=approval_scope,
    )


def _legacy_approval_scope(
    pending: dict[str, Any],
    action: object | None,
) -> str:
    configured_mode = getattr(getattr(action, "approval", None), "mode", None)
    return (
        "session"
        if pending.get("approval_scope") == "session_tool"
        or getattr(configured_mode, "value", None) == "session_tool"
        else "once"
    )


def _required_nonempty_string(data: dict[str, Any], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"pending approval {field} must be a non-empty string")
    return value


def _persisted_taint(pending: dict[str, Any], field: str) -> bool:
    """Fail closed when durable request-time taint evidence is missing or malformed."""
    value = pending.get(field)
    return value if type(value) is bool else True


def _persisted_secret_taint(pending: dict[str, Any]) -> bool:
    """Read durable redaction state and reject invalid values."""
    marker = pending.get("redaction_required")
    if marker is None:
        return _persisted_taint(pending, "secret_tainted")
    return not isinstance(marker, str) or marker != "not_required"
