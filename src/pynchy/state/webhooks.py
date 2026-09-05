"""Atomic webhook receipt and one-time task admission."""

from __future__ import annotations

from dataclasses import replace

from aiosqlite import (  # noqa: TC002 - beartype resolves state boundary annotations at runtime.
    Connection,
    Row,
)

from pynchy.conversation.api import ConversationDeliveryStatus
from pynchy.scheduling.api import (
    ScheduledTask,  # noqa: TC001 - beartype resolves admission annotations at runtime.
)
from pynchy.state.connection import _get_db, atomic_write
from pynchy.state.conversation_controls import (
    _apply_conversation_control_state,
    _retire_conversation_for_terminal,
)
from pynchy.state.conversation_routing import _admit_conversation_delivery
from pynchy.state.tasks import _insert_task, _row_to_task
from pynchy.state.webhook_effect_admission import admit_webhook_effect_delivery
from pynchy.state.webhook_models import (
    WebhookAdmission,
    WebhookConversationAdmission,
    WebhookConversationRequest,
    WebhookReceipt,
)
from pynchy.webhook_effects import (  # noqa: TC001 - beartype resolves admission evidence.
    WebhookEffectEvidence,
)


def _row_to_receipt(row: Row) -> WebhookReceipt:
    return WebhookReceipt(
        provider=row["provider"],
        route=row["route"],
        delivery_id=row["delivery_id"],
        workspace=row["workspace"],
        event_type=row["event_type"],
        event_action=row["event_action"],
        subject_id=row["subject_id"],
        payload_sha256=row["payload_sha256"],
        disposition=row["disposition"],
        ignored_reason=row["ignored_reason"],
        task_id=row["task_id"],
        occurred_at=row["occurred_at"],
        received_at=row["received_at"],
    )


async def _effect_decision(
    database: Connection,
    receipt: WebhookReceipt,
) -> str | None:
    cursor = await database.execute(
        """
        SELECT decision FROM webhook_effect_decisions
        WHERE provider = ? AND route = ? AND delivery_id = ?
        """,
        (receipt.provider, receipt.route, receipt.delivery_id),
    )
    row = await cursor.fetchone()
    return row["decision"] if row is not None else None


async def _existing_admission(
    database: Connection,
    receipt: WebhookReceipt,
) -> WebhookAdmission | None:
    cursor = await database.execute(
        """
        SELECT * FROM webhook_receipts
        WHERE provider = ? AND route = ? AND delivery_id = ?
        """,
        (receipt.provider, receipt.route, receipt.delivery_id),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    existing = _row_to_receipt(row)
    if existing.payload_sha256 != receipt.payload_sha256:
        raise ValueError("Webhook delivery identity has conflicting receipt evidence")
    task_row = None
    if existing.task_id is not None:
        task_cursor = await database.execute(
            "SELECT * FROM scheduled_tasks WHERE id = ?",
            (existing.task_id,),
        )
        task_row = await task_cursor.fetchone()
    decision = await _effect_decision(database, existing)
    return WebhookAdmission(
        receipt=existing,
        task=_row_to_task(task_row) if task_row is not None else None,
        created=False,
        outbound_effect_suppressed=decision == "suppressed",
        outbound_effect_held=decision == "held",
    )


async def _ensure_external_receipt(database: Connection, receipt: WebhookReceipt) -> None:
    await database.execute(
        """
        INSERT OR IGNORE INTO external_receipts (
            provider, route, delivery_id, payload_sha256, received_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            receipt.provider,
            receipt.route,
            receipt.delivery_id,
            receipt.payload_sha256,
            receipt.received_at,
        ),
    )
    cursor = await database.execute(
        """
        SELECT payload_sha256 FROM external_receipts
        WHERE provider = ? AND route = ? AND delivery_id = ?
        """,
        (receipt.provider, receipt.route, receipt.delivery_id),
    )
    admitted = await cursor.fetchone()
    if admitted is None or admitted["payload_sha256"] != receipt.payload_sha256:
        raise ValueError("Webhook delivery identity has conflicting receipt evidence")


async def _insert_receipt(database: Connection, receipt: WebhookReceipt) -> None:
    await database.execute(
        """
        INSERT INTO webhook_receipts (
            provider, route, delivery_id, workspace, event_type, event_action,
            subject_id, payload_sha256, disposition, ignored_reason, task_id,
            occurred_at, received_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            receipt.provider,
            receipt.route,
            receipt.delivery_id,
            receipt.workspace,
            receipt.event_type,
            receipt.event_action,
            receipt.subject_id,
            receipt.payload_sha256,
            receipt.disposition,
            receipt.ignored_reason,
            receipt.task_id,
            receipt.occurred_at,
            receipt.received_at,
        ),
    )


async def _admit_webhook_receipt(
    database: Connection,
    receipt: WebhookReceipt,
    task: ScheduledTask | None,
    *,
    effect_evidence: WebhookEffectEvidence | None = None,
) -> WebhookAdmission:
    if receipt.task_id != (task.id if task is not None else None):
        raise ValueError("Webhook receipt task identity does not match its admitted task")
    if effect_evidence is not None and task is not None:
        raise ValueError("Outbound-effect callbacks cannot create isolated tasks")

    await _ensure_external_receipt(database, receipt)
    existing = await _existing_admission(database, receipt)
    if existing is not None:
        return existing
    if task is not None:
        await _insert_task(database, task)
    await _insert_receipt(database, receipt)
    suppressed = False
    held = False
    if effect_evidence is not None and receipt.disposition != "lifecycle":
        suppressed, held = await admit_webhook_effect_delivery(
            database,
            receipt,
            effect_evidence,
        )
    return WebhookAdmission(
        receipt=receipt,
        task=task,
        created=True,
        outbound_effect_suppressed=suppressed,
        outbound_effect_held=held,
    )


async def admit_webhook_receipt(
    receipt: WebhookReceipt,
    task: ScheduledTask | None,
    *,
    effect_evidence: WebhookEffectEvidence | None = None,
) -> WebhookAdmission:
    """Persist one immutable receipt and its callback-correlation decision."""
    async with atomic_write() as database:
        return await _admit_webhook_receipt(
            database,
            receipt,
            task,
            effect_evidence=effect_evidence,
        )


async def admit_webhook_conversation(
    receipt: WebhookReceipt,
    request: WebhookConversationRequest,
    *,
    effect_evidence: WebhookEffectEvidence | None = None,
) -> WebhookConversationAdmission:
    """Atomically persist a routed receipt, correlation, payload, and FIFO state."""
    if receipt.disposition not in {"routed", "lifecycle"}:
        raise ValueError("Conversation webhook admission requires a routed receipt")
    async with atomic_write() as database:
        webhook = await _admit_webhook_receipt(
            database,
            receipt,
            None,
            effect_evidence=effect_evidence,
        )
        if webhook.receipt.disposition not in {"routed", "lifecycle"}:
            return WebhookConversationAdmission(
                webhook=webhook,
                conversation=None,
            )
        conversation = await _admit_conversation_delivery(
            database,
            request.identity,
            request.subject,
            request.workspace,
            payload=request.payload,
        )
        if (
            conversation is not None
            and conversation.delivery.status is not ConversationDeliveryStatus.COMPLETED
        ):
            if webhook.receipt.disposition == "lifecycle":
                conversation = replace(
                    conversation,
                    terminal_retirement=await _retire_conversation_for_terminal(
                        database,
                        conversation.conversation.id,
                        preserve_delivery=conversation.delivery.identity,
                        control_state_revision=request.control_state_revision,
                    ),
                )
            elif request.control_closed is not None:
                await _apply_conversation_control_state(
                    database,
                    conversation.conversation.id,
                    closed=request.control_closed,
                    control_state_revision=request.control_state_revision,
                )
        return WebhookConversationAdmission(
            webhook=webhook,
            conversation=conversation,
        )


async def get_webhook_receipt(
    provider: str,
    route: str,
    delivery_id: str,
) -> WebhookReceipt | None:
    """Return one receipt by its provider delivery identity."""
    database = _get_db()
    cursor = await database.execute(
        """
        SELECT * FROM webhook_receipts
        WHERE provider = ? AND route = ? AND delivery_id = ?
        """,
        (provider, route, delivery_id),
    )
    row = await cursor.fetchone()
    return _row_to_receipt(row) if row is not None else None
