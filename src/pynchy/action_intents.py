"""Domain types for durable external-action request and receipt state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ActionIntentStatus(StrEnum):
    """Durable lifecycle for an external write requested by an agent."""

    DRAFTED = "drafted"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    CLAIMED = "claimed"
    EXECUTING = "executing"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    DENIED = "denied"
    EXPIRED = "expired"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True)
class ActionIntent:
    """Canonical external-write request, its authorization, and provider outcome."""

    id: str
    request_id: str
    workspace: str
    action_id: str
    tool_name: str
    provider: str
    actor_jid: str
    recipient: str
    payload: dict[str, Any]
    source_refs: tuple[str, ...]
    summary: str
    policy_decision: str
    approver: str | None
    approved_at: str | None
    status: ActionIntentStatus
    claimed_at: str | None
    execution_started_at: str | None
    attempts: int
    provider_request_id: str | None
    provider_receipt: dict[str, Any] | None
    error: str | None
    created_at: str
    updated_at: str
    resolved_at: str | None
