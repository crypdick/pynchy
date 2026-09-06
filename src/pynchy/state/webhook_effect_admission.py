"""Inbound admission against the durable outbound-effect ledger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pynchy.conversation.api import (
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.state.connection import _get_db
from pynchy.state.webhook_effect_decisions import (
    set_webhook_effect_decision,
    webhook_effect_decision,
)
from pynchy.state.webhook_models import (
    WebhookReceipt,
)
from pynchy.webhook_effects import (
    WebhookEffectCallbackDecision,
    WebhookEffectEvidence,
)

if TYPE_CHECKING:
    from aiosqlite import Connection, Row
else:
    Connection = Any
    Row = Any

_CALLBACK_CANDIDATE_HORIZON = timedelta(hours=24)
_SELF_EFFECT_REASON = "pynchy_outbound_effect_echo"


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _effect_can_have_produced(row: Row, occurred_at: str) -> bool:
    created_at = _parse_timestamp(row["created_at"]).astimezone(UTC)
    callback_at = _parse_timestamp(occurred_at).astimezone(UTC)
    return (
        created_at - timedelta(minutes=5)
        <= callback_at
        <= (created_at + _CALLBACK_CANDIDATE_HORIZON)
    )


def _validate_receipt_evidence(
    receipt: WebhookReceipt,
    evidence: WebhookEffectEvidence,
) -> None:
    scope = evidence.scope
    if (
        receipt.provider != scope.provider
        or receipt.event_type != scope.event_type
        or receipt.event_action != scope.event_action
        or receipt.subject_id != scope.subject_id
    ):
        raise ValueError("Webhook effect evidence does not match its receipt")


async def _matching_webhook_effects(
    database: Connection,
    evidence: WebhookEffectEvidence,
    occurred_at: str,
) -> tuple[bool, list[Row]]:
    exact_cursor = await database.execute(
        """
        SELECT 1 FROM webhook_effects
        WHERE provider = ? AND account = ? AND event_type = ? AND event_action = ?
          AND subject_id = ? AND status = 'confirmed' AND fingerprint = ?
        LIMIT 1
        """,
        (
            evidence.scope.provider,
            evidence.scope.account,
            evidence.scope.event_type,
            evidence.scope.event_action,
            evidence.scope.subject_id,
            evidence.fingerprint,
        ),
    )
    if await exact_cursor.fetchone() is not None:
        return True, []
    pending_cursor = await database.execute(
        """
        SELECT * FROM webhook_effects
        WHERE provider = ? AND account = ? AND event_type = ? AND event_action = ?
          AND (subject_id IS NULL OR subject_id = ?)
          AND status IN ('prepared', 'executing', 'outcome_unknown')
        """,
        (
            evidence.scope.provider,
            evidence.scope.account,
            evidence.scope.event_type,
            evidence.scope.event_action,
            evidence.scope.subject_id,
        ),
    )
    candidates = [
        row
        for row in await pending_cursor.fetchall()
        if _effect_can_have_produced(row, occurred_at)
    ]
    return False, candidates


async def classify_webhook_effect_callback(
    evidence: WebhookEffectEvidence,
    occurred_at: str,
) -> WebhookEffectCallbackDecision:
    """Classify before route effects; durable admission repeats this authoritatively."""
    exact, candidates = await _matching_webhook_effects(_get_db(), evidence, occurred_at)
    if exact:
        return WebhookEffectCallbackDecision.SUPPRESSED
    if candidates:
        return WebhookEffectCallbackDecision.HELD
    return WebhookEffectCallbackDecision.UNRELATED


async def admit_webhook_effect_delivery(
    database: Connection,
    receipt: WebhookReceipt,
    evidence: WebhookEffectEvidence,
) -> tuple[bool, bool]:
    """Classify one callback as retained-exact, held-candidate, or ordinary."""
    _validate_receipt_evidence(receipt, evidence)
    identity = ExternalDeliveryIdentity(
        provider=ExternalProvider(receipt.provider),
        route=ExternalRoute(receipt.route),
        delivery_id=ExternalDeliveryId(receipt.delivery_id),
    )
    existing = await webhook_effect_decision(database, identity)
    if existing is not None:
        return existing == "suppressed", existing == "held"
    exact, candidates = await _matching_webhook_effects(
        database,
        evidence,
        receipt.occurred_at,
    )
    if exact:
        await set_webhook_effect_decision(
            database,
            identity,
            "suppressed",
            reason=_SELF_EFFECT_REASON,
        )
        return True, False
    for candidate in candidates:
        await database.execute(
            """
            INSERT OR IGNORE INTO webhook_effect_candidates (
                provider, route, delivery_id, effect_id, fingerprint
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                receipt.provider,
                receipt.route,
                receipt.delivery_id,
                candidate["id"],
                evidence.fingerprint,
            ),
        )
    if candidates:
        await set_webhook_effect_decision(database, identity, "held")
        return False, True
    return False, False
