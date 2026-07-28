"""Durable correlation between outbound effects and inbound webhook deliveries."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pynchy.conversation.api import (
    ConversationDeliveryCompletion,
    ConversationId,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.logger import logger
from pynchy.state.connection import _get_db, atomic_write
from pynchy.state.webhook_effect_decisions import set_webhook_effect_decision
from pynchy.webhook_effects import (
    WebhookEffect,
    WebhookEffectEvidence,
    WebhookEffectId,
    WebhookEffectResolution,
    WebhookEffectScope,
    WebhookEffectStatus,
)

if TYPE_CHECKING:
    from aiosqlite import Connection, Row
else:
    Connection = Any
    Row = Any

_CONFIRMED_RETENTION = timedelta(days=7)
_SELF_EFFECT_REASON = "pynchy_outbound_effect_echo"


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


async def _prune_resolved_effects(database: Connection) -> None:
    cutoff = (datetime.now(UTC) - _CONFIRMED_RETENTION).isoformat()
    await database.execute(
        """
        DELETE FROM webhook_effects
        WHERE status IN ('confirmed', 'failed', 'reconciled_absent')
          AND resolved_at < ?
          AND NOT EXISTS (
              SELECT 1 FROM webhook_effect_candidates
              WHERE webhook_effect_candidates.effect_id = webhook_effects.id
          )
        """,
        (cutoff,),
    )


async def begin_webhook_effect(scope: WebhookEffectScope) -> WebhookEffectId:
    """Persist provider mutation intent before external I/O starts."""
    effect_id = WebhookEffectId(f"effect_{secrets.token_urlsafe(18)}")
    async with atomic_write() as database:
        await _prune_resolved_effects(database)
        await database.execute(
            """
            INSERT INTO webhook_effects (
                id, provider, account, event_type, event_action, subject_id,
                intent_fingerprint, status, fingerprint, created_at, executing_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared', NULL, ?, NULL, NULL)
            """,
            (
                effect_id,
                scope.provider,
                scope.account,
                scope.event_type,
                scope.event_action,
                scope.subject_id,
                scope.intent_fingerprint,
                _timestamp(),
            ),
        )
    return effect_id


async def mark_webhook_effect_executing(effect_id: WebhookEffectId) -> None:
    """Mark the point after which provider outcome may be unknown on failure."""
    async with atomic_write() as database:
        cursor = await database.execute(
            """
            UPDATE webhook_effects
            SET status = 'executing', executing_at = ?
            WHERE id = ? AND status = 'prepared'
            """,
            (_timestamp(), effect_id),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"Webhook effect is not prepared: {effect_id}")


async def _effect_row(database: Connection, effect_id: WebhookEffectId) -> Row:
    cursor = await database.execute(
        "SELECT * FROM webhook_effects WHERE id = ?",
        (effect_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise ValueError(f"Unknown webhook effect: {effect_id}")
    return row


def _row_to_effect(row: Row) -> WebhookEffect:
    return WebhookEffect(
        id=WebhookEffectId(row["id"]),
        scope=WebhookEffectScope(
            provider=row["provider"],
            account=row["account"],
            event_type=row["event_type"],
            event_action=row["event_action"],
            subject_id=row["subject_id"],
            intent_fingerprint=row["intent_fingerprint"],
        ),
        status=WebhookEffectStatus(row["status"]),
        fingerprint=row["fingerprint"],
        created_at=row["created_at"],
        executing_at=row["executing_at"],
        resolved_at=row["resolved_at"],
    )


async def list_webhook_effects(
    *,
    status: WebhookEffectStatus | None = None,
    limit: int = 100,
) -> list[WebhookEffect]:
    """Return bounded operator projections, newest first."""
    if not 1 <= limit <= 200:
        raise ValueError("Webhook effect limit must be from 1 to 200")
    database = _get_db()
    if status is None:
        cursor = await database.execute(
            "SELECT * FROM webhook_effects ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        )
    else:
        cursor = await database.execute(
            """
            SELECT * FROM webhook_effects
            WHERE status = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (status.value, limit),
        )
    return [_row_to_effect(row) for row in await cursor.fetchall()]


def _validate_confirmation(row: Row, evidence: WebhookEffectEvidence) -> None:
    scope = evidence.scope
    if (
        row["provider"] != scope.provider
        or row["account"] != scope.account
        or row["event_type"] != scope.event_type
        or row["event_action"] != scope.event_action
        or (row["subject_id"] is not None and row["subject_id"] != scope.subject_id)
    ):
        raise ValueError("Webhook effect confirmation does not match its intent")


def _completion(row: Row) -> ConversationDeliveryCompletion:
    return ConversationDeliveryCompletion(
        identity=ExternalDeliveryIdentity(
            provider=ExternalProvider(row["provider"]),
            route=ExternalRoute(row["route"]),
            delivery_id=ExternalDeliveryId(row["delivery_id"]),
        ),
        conversation_id=ConversationId(row["conversation_id"]),
    )


async def _resolve_candidate(
    database: Connection,
    candidate: Row,
    *,
    matched: bool,
) -> ConversationDeliveryCompletion | None:
    identity = ExternalDeliveryIdentity(
        provider=ExternalProvider(candidate["provider"]),
        route=ExternalRoute(candidate["route"]),
        delivery_id=ExternalDeliveryId(candidate["delivery_id"]),
    )
    if matched:
        await set_webhook_effect_decision(
            database,
            identity,
            "suppressed",
            reason=_SELF_EFFECT_REASON,
        )
        await database.execute(
            """
            DELETE FROM webhook_effect_candidates
            WHERE provider = ? AND route = ? AND delivery_id = ?
            """,
            (identity.provider, identity.route, identity.delivery_id),
        )
        cursor = await database.execute(
            """
            UPDATE conversation_deliveries
            SET status = 'completed', completed_at = ?
            WHERE provider = ? AND route = ? AND delivery_id = ? AND status = 'held'
            """,
            (_timestamp(), identity.provider, identity.route, identity.delivery_id),
        )
    else:
        await database.execute(
            """
            DELETE FROM webhook_effect_candidates
            WHERE provider = ? AND route = ? AND delivery_id = ? AND effect_id = ?
            """,
            (
                identity.provider,
                identity.route,
                identity.delivery_id,
                candidate["effect_id"],
            ),
        )
        remaining_cursor = await database.execute(
            """
            SELECT 1 FROM webhook_effect_candidates
            WHERE provider = ? AND route = ? AND delivery_id = ?
            LIMIT 1
            """,
            (identity.provider, identity.route, identity.delivery_id),
        )
        if await remaining_cursor.fetchone() is not None:
            return None
        await set_webhook_effect_decision(database, identity, "released")
        cursor = await database.execute(
            """
            UPDATE conversation_deliveries
            SET status = 'pending'
            WHERE provider = ? AND route = ? AND delivery_id = ? AND status = 'held'
            """,
            (identity.provider, identity.route, identity.delivery_id),
        )
    if cursor.rowcount != 1 or candidate["conversation_id"] is None:
        return None
    return _completion(candidate)


async def _effect_candidates(database: Connection, effect_id: WebhookEffectId) -> list[Row]:
    cursor = await database.execute(
        """
        SELECT candidates.*, deliveries.conversation_id
        FROM webhook_effect_candidates AS candidates
        LEFT JOIN conversation_deliveries AS deliveries
          ON deliveries.provider = candidates.provider
         AND deliveries.route = candidates.route
         AND deliveries.delivery_id = candidates.delivery_id
        WHERE candidates.effect_id = ?
        """,
        (effect_id,),
    )
    return list(await cursor.fetchall())


async def confirm_webhook_effect(
    effect_id: WebhookEffectId,
    evidence: WebhookEffectEvidence,
) -> WebhookEffectResolution:
    """Confirm exact provider evidence and suppress every matching route delivery."""
    wakeups: list[ConversationDeliveryCompletion] = []
    async with atomic_write() as database:
        row = await _effect_row(database, effect_id)
        _validate_confirmation(row, evidence)
        if row["status"] == "confirmed":
            if row["fingerprint"] != evidence.fingerprint:
                raise ValueError("Webhook effect was confirmed with different evidence")
            return WebhookEffectResolution()
        if row["status"] not in {"executing", "outcome_unknown"}:
            raise ValueError(f"Webhook effect cannot be confirmed from {row['status']}")
        await database.execute(
            """
            UPDATE webhook_effects
            SET status = 'confirmed', subject_id = ?, fingerprint = ?, resolved_at = ?
            WHERE id = ?
            """,
            (evidence.scope.subject_id, evidence.fingerprint, _timestamp(), effect_id),
        )
        for candidate in await _effect_candidates(database, effect_id):
            wakeup = await _resolve_candidate(
                database,
                candidate,
                matched=candidate["fingerprint"] == evidence.fingerprint,
            )
            if wakeup is not None:
                wakeups.append(wakeup)
    return WebhookEffectResolution(tuple(wakeups))


async def _resolve_webhook_effect_absent(
    effect_id: WebhookEffectId,
    *,
    expected: set[str],
    status: WebhookEffectStatus,
) -> WebhookEffectResolution:
    wakeups: list[ConversationDeliveryCompletion] = []
    async with atomic_write() as database:
        row = await _effect_row(database, effect_id)
        if row["status"] == status.value:
            return WebhookEffectResolution()
        if row["status"] not in expected:
            raise ValueError(f"Webhook effect cannot become {status.value} from {row['status']}")
        await database.execute(
            "UPDATE webhook_effects SET status = ?, resolved_at = ? WHERE id = ?",
            (status.value, _timestamp(), effect_id),
        )
        for candidate in await _effect_candidates(database, effect_id):
            wakeup = await _resolve_candidate(database, candidate, matched=False)
            if wakeup is not None:
                wakeups.append(wakeup)
    return WebhookEffectResolution(tuple(wakeups))


async def fail_webhook_effect(effect_id: WebhookEffectId) -> WebhookEffectResolution:
    """Release candidates only after a provider-declared mutation failure."""
    return await _resolve_webhook_effect_absent(
        effect_id,
        expected={"prepared", "executing"},
        status=WebhookEffectStatus.FAILED,
    )


async def reconcile_webhook_effect_absent(
    effect_id: WebhookEffectId,
) -> WebhookEffectResolution:
    """Release an unknown effect after an operator proves it did not occur."""
    return await _resolve_webhook_effect_absent(
        effect_id,
        expected={"outcome_unknown"},
        status=WebhookEffectStatus.RECONCILED_ABSENT,
    )


async def mark_webhook_effect_outcome_unknown(effect_id: WebhookEffectId) -> None:
    """Quarantine candidates when provider success cannot be disproved."""
    async with atomic_write() as database:
        row = await _effect_row(database, effect_id)
        if row["status"] == "outcome_unknown":
            return
        if row["status"] != "executing":
            raise ValueError(f"Webhook effect outcome cannot be unknown from {row['status']}")
        await database.execute(
            "UPDATE webhook_effects SET status = 'outcome_unknown' WHERE id = ?",
            (effect_id,),
        )
    logger.warning(
        "Webhook effect outcome requires reconciliation",
        effect_id=effect_id,
        provider=row["provider"],
        account=row["account"],
        event_type=row["event_type"],
        event_action=row["event_action"],
        subject_id=row["subject_id"],
    )


async def recover_incomplete_webhook_effects() -> WebhookEffectResolution:
    """Fail unsent effects and quarantine effects that may have reached a provider."""
    wakeups: list[ConversationDeliveryCompletion] = []
    async with atomic_write() as database:
        cursor = await database.execute(
            "SELECT id, status FROM webhook_effects WHERE status IN ('prepared', 'executing')"
        )
        effects = list(await cursor.fetchall())
    for effect in effects:
        effect_id = WebhookEffectId(effect["id"])
        if effect["status"] == "prepared":
            wakeups.extend((await fail_webhook_effect(effect_id)).wakeups)
        else:
            await mark_webhook_effect_outcome_unknown(effect_id)
    return WebhookEffectResolution(tuple(wakeups))
