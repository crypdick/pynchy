"""Durable, fail-closed lifecycle records for external agent actions."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from pynchy.action_intents import ActionIntent, ActionIntentStatus
from pynchy.state.connection import _get_db, atomic_write


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class ActionIntentCreateRequest:
    """Canonical data required to persist an external action draft."""

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


@dataclass(frozen=True)
class ActionIntentStatusUpdate:
    """One guarded lifecycle transition and its optional evidence."""

    request_id: str
    expected: tuple[ActionIntentStatus, ...]
    status: ActionIntentStatus
    policy_decision: str | None = None
    approver: str | None = None
    approved_at: str | None = None
    execution_started_at: str | None = None
    provider_request_id: str | None = None
    provider_receipt: str | None = None
    resolved_at: str | None = None


def _row_to_action_intent(row: aiosqlite.Row) -> ActionIntent:
    payload = json.loads(row["payload"])
    receipt = json.loads(row["provider_receipt"]) if row["provider_receipt"] else None
    source_refs = json.loads(row["source_refs"])
    return ActionIntent(
        id=row["id"],
        request_id=row["request_id"],
        workspace=row["workspace"],
        action_id=row["action_id"],
        tool_name=row["tool_name"],
        provider=row["provider"],
        actor_jid=row["actor_jid"],
        recipient=row["recipient"],
        payload=payload,
        source_refs=tuple(source_refs),
        summary=row["summary"],
        policy_decision=row["policy_decision"],
        approver=row["approver"],
        approved_at=row["approved_at"],
        status=ActionIntentStatus(row["status"]),
        claimed_at=row["claimed_at"],
        execution_started_at=row["execution_started_at"],
        attempts=row["attempts"],
        provider_request_id=row["provider_request_id"],
        provider_receipt=receipt,
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        resolved_at=row["resolved_at"],
    )


async def get_action_intent_by_request(request_id: str) -> ActionIntent | None:
    """Return the intent owned by one IPC idempotency key."""
    db = _get_db()
    cursor = await db.execute("SELECT * FROM action_intents WHERE request_id = ?", (request_id,))
    row = await cursor.fetchone()
    return _row_to_action_intent(row) if row else None


async def list_action_intents(
    *, workspace: str | None = None, limit: int = 100
) -> list[ActionIntent]:
    """Return bounded operator records, newest first."""
    db = _get_db()
    if workspace:
        cursor = await db.execute(
            """
            SELECT * FROM action_intents WHERE workspace = ?
            ORDER BY updated_at DESC, id DESC LIMIT ?
            """,
            (workspace, limit),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM action_intents ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,)
        )
    return [_row_to_action_intent(row) for row in await cursor.fetchall()]


def action_intent_to_dict(intent: ActionIntent) -> dict[str, object]:
    """Return a control-plane projection without the external message body."""
    return {
        "id": intent.id,
        "request_id": intent.request_id,
        "workspace": intent.workspace,
        "action_id": intent.action_id,
        "tool_name": intent.tool_name,
        "provider": intent.provider,
        "actor_jid": intent.actor_jid,
        "recipient": intent.recipient,
        "summary": intent.summary,
        "policy_decision": intent.policy_decision,
        "approver": intent.approver,
        "approved_at": intent.approved_at,
        "status": intent.status.value,
        "claimed_at": intent.claimed_at,
        "execution_started_at": intent.execution_started_at,
        "attempts": intent.attempts,
        "provider_request_id": intent.provider_request_id,
        "provider_receipt": intent.provider_receipt,
        "error": intent.error,
        "created_at": intent.created_at,
        "updated_at": intent.updated_at,
        "resolved_at": intent.resolved_at,
    }


async def create_action_intent(request: ActionIntentCreateRequest) -> tuple[ActionIntent, bool]:
    """Persist a canonical draft, returning an existing request unchanged."""
    now = _timestamp()
    intent = ActionIntent(
        id=uuid.uuid4().hex,
        request_id=request.request_id,
        workspace=request.workspace,
        action_id=request.action_id,
        tool_name=request.tool_name,
        provider=request.provider,
        actor_jid=request.actor_jid,
        recipient=request.recipient,
        payload=request.payload,
        source_refs=request.source_refs,
        summary=request.summary,
        policy_decision="pending",
        approver=None,
        approved_at=None,
        status=ActionIntentStatus.DRAFTED,
        claimed_at=None,
        execution_started_at=None,
        attempts=0,
        provider_request_id=None,
        provider_receipt=None,
        error=None,
        created_at=now,
        updated_at=now,
        resolved_at=None,
    )
    try:
        async with atomic_write() as db:
            await db.execute(
                """
                INSERT INTO action_intents (
                    id, request_id, workspace, action_id, tool_name, provider, actor_jid,
                    recipient, payload, source_refs, summary, policy_decision, approver,
                    approved_at, status, claimed_at, execution_started_at, attempts,
                    provider_request_id, provider_receipt, error, created_at, updated_at,
                    resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent.id,
                    intent.request_id,
                    intent.workspace,
                    intent.action_id,
                    intent.tool_name,
                    intent.provider,
                    intent.actor_jid,
                    intent.recipient,
                    json.dumps(intent.payload, sort_keys=True),
                    json.dumps(intent.source_refs),
                    intent.summary,
                    intent.policy_decision,
                    intent.approver,
                    intent.approved_at,
                    intent.status.value,
                    intent.claimed_at,
                    intent.execution_started_at,
                    intent.attempts,
                    intent.provider_request_id,
                    None,
                    intent.error,
                    intent.created_at,
                    intent.updated_at,
                    intent.resolved_at,
                ),
            )
    except aiosqlite.IntegrityError:
        existing = await get_action_intent_by_request(request.request_id)
        if existing is not None:
            return existing, False
        raise
    return intent, True


async def mark_action_intent_awaiting_approval(
    request_id: str, *, policy_decision: str
) -> ActionIntent:
    """Record that the immutable draft is waiting for a human decision."""
    return await _set_action_intent_status(
        ActionIntentStatusUpdate(
            request_id=request_id,
            expected=(ActionIntentStatus.DRAFTED,),
            status=ActionIntentStatus.AWAITING_APPROVAL,
            policy_decision=policy_decision,
        )
    )


async def approve_action_intent(
    request_id: str, *, approver: str, approved_at: str, policy_decision: str
) -> ActionIntent:
    """Record a human or policy approval before claiming provider execution."""
    existing = await get_action_intent_by_request(request_id)
    if existing is not None and existing.status is ActionIntentStatus.APPROVED:
        return existing
    return await _set_action_intent_status(
        ActionIntentStatusUpdate(
            request_id=request_id,
            expected=(ActionIntentStatus.DRAFTED, ActionIntentStatus.AWAITING_APPROVAL),
            status=ActionIntentStatus.APPROVED,
            policy_decision=policy_decision,
            approver=approver,
            approved_at=approved_at,
        )
    )


async def deny_action_intent(request_id: str, *, reason: str) -> ActionIntent | None:
    """Close an unexecuted draft without treating it as a provider outcome."""
    return await _close_action_intent(
        request_id,
        expected=(
            ActionIntentStatus.DRAFTED,
            ActionIntentStatus.AWAITING_APPROVAL,
            ActionIntentStatus.APPROVED,
        ),
        status=ActionIntentStatus.DENIED,
        error=reason,
    )


async def expire_action_intent(request_id: str, *, reason: str) -> ActionIntent | None:
    """Close an unapproved draft whose approval window elapsed."""
    return await _close_action_intent(
        request_id,
        expected=(ActionIntentStatus.DRAFTED, ActionIntentStatus.AWAITING_APPROVAL),
        status=ActionIntentStatus.EXPIRED,
        error=reason,
    )


async def fail_action_intent(request_id: str, *, reason: str) -> ActionIntent | None:
    """Close an unexecuted intent after a known local failure."""
    return await _close_action_intent(
        request_id,
        expected=(
            ActionIntentStatus.DRAFTED,
            ActionIntentStatus.AWAITING_APPROVAL,
            ActionIntentStatus.APPROVED,
        ),
        status=ActionIntentStatus.FAILED,
        error=reason,
    )


async def claim_action_intent(request_id: str) -> ActionIntent | None:
    """Atomically claim one approved intent; competing calls never execute it twice."""
    now = _timestamp()
    async with atomic_write() as db:
        cursor = await db.execute(
            """
            UPDATE action_intents
            SET status = ?, claimed_at = ?, attempts = attempts + 1, updated_at = ?
            WHERE request_id = ? AND status = ?
            """,
            (
                ActionIntentStatus.CLAIMED.value,
                now,
                now,
                request_id,
                ActionIntentStatus.APPROVED.value,
            ),
        )
        if cursor.rowcount != 1:
            return await _get_action_intent_by_request_in_transaction(db, request_id)
    return await get_action_intent_by_request(request_id)


async def mark_action_intent_executing(request_id: str) -> ActionIntent:
    """Persist the provider-attempt boundary before invoking an external client."""
    return await _set_action_intent_status(
        ActionIntentStatusUpdate(
            request_id=request_id,
            expected=(ActionIntentStatus.CLAIMED,),
            status=ActionIntentStatus.EXECUTING,
            execution_started_at=_timestamp(),
        )
    )


async def confirm_action_intent(
    request_id: str, *, provider_request_id: str, receipt: dict[str, Any]
) -> ActionIntent:
    """Persist the provider receipt that proves an external write completed."""
    now = _timestamp()
    return await _set_action_intent_status(
        ActionIntentStatusUpdate(
            request_id=request_id,
            expected=(ActionIntentStatus.EXECUTING,),
            status=ActionIntentStatus.CONFIRMED,
            provider_request_id=provider_request_id,
            provider_receipt=json.dumps(receipt, sort_keys=True),
            resolved_at=now,
        )
    )


async def mark_action_intent_outcome_unknown(
    request_id: str, *, reason: str
) -> ActionIntent | None:
    """Fail closed after a provider attempt lacks a durable receipt."""
    return await _close_action_intent(
        request_id,
        expected=(ActionIntentStatus.CLAIMED, ActionIntentStatus.EXECUTING),
        status=ActionIntentStatus.OUTCOME_UNKNOWN,
        error=reason,
    )


async def recover_incomplete_action_intents() -> int:
    """Fail closed on startup for actions interrupted around provider execution."""
    now = _timestamp()
    async with atomic_write() as db:
        cursor = await db.execute(
            """
            UPDATE action_intents
            SET status = ?, error = ?, updated_at = ?, resolved_at = ?
            WHERE status IN (?, ?)
            """,
            (
                ActionIntentStatus.OUTCOME_UNKNOWN.value,
                "Pynchy stopped before recording a provider receipt; reconcile before retrying.",
                now,
                now,
                ActionIntentStatus.CLAIMED.value,
                ActionIntentStatus.EXECUTING.value,
            ),
        )
    return cursor.rowcount


async def _set_action_intent_status(update: ActionIntentStatusUpdate) -> ActionIntent:
    now = _timestamp()
    placeholders = ", ".join("?" for _ in update.expected)
    async with atomic_write() as db:
        cursor = await db.execute(
            f"""
            UPDATE action_intents
            SET status = ?,
                policy_decision = COALESCE(?, policy_decision),
                approver = COALESCE(?, approver),
                approved_at = COALESCE(?, approved_at),
                execution_started_at = COALESCE(?, execution_started_at),
                provider_request_id = COALESCE(?, provider_request_id),
                provider_receipt = COALESCE(?, provider_receipt),
                updated_at = ?,
                resolved_at = COALESCE(?, resolved_at)
            WHERE request_id = ? AND status IN ({placeholders})
            """,  # noqa: S608 - expected state placeholders are enum-controlled.
            (
                update.status.value,
                update.policy_decision,
                update.approver,
                update.approved_at,
                update.execution_started_at,
                update.provider_request_id,
                update.provider_receipt,
                now,
                update.resolved_at,
                update.request_id,
                *(item.value for item in update.expected),
            ),
        )
        if cursor.rowcount != 1:
            existing = await _get_action_intent_by_request_in_transaction(db, update.request_id)
            if existing is None:
                raise RuntimeError(f"Action intent not found: {update.request_id}")
            raise RuntimeError(
                f"Action intent {update.request_id} cannot move from "
                f"{existing.status.value} to {update.status.value}"
            )
    intent = await get_action_intent_by_request(update.request_id)
    if intent is None:
        raise RuntimeError(f"Action intent disappeared: {update.request_id}")
    return intent


async def _close_action_intent(
    request_id: str,
    *,
    expected: tuple[ActionIntentStatus, ...],
    status: ActionIntentStatus,
    error: str,
) -> ActionIntent | None:
    now = _timestamp()
    placeholders = ", ".join("?" for _ in expected)
    async with atomic_write() as db:
        await db.execute(
            f"""
            UPDATE action_intents
            SET status = ?, error = ?, updated_at = ?, resolved_at = ?
            WHERE request_id = ? AND status IN ({placeholders})
            """,  # noqa: S608 - expected state placeholders are enum-controlled.
            (status.value, error, now, now, request_id, *(item.value for item in expected)),
        )
    return await get_action_intent_by_request(request_id)


async def _get_action_intent_by_request_in_transaction(
    db: aiosqlite.Connection, request_id: str
) -> ActionIntent | None:
    cursor = await db.execute("SELECT * FROM action_intents WHERE request_id = ?", (request_id,))
    row = await cursor.fetchone()
    return _row_to_action_intent(row) if row else None
