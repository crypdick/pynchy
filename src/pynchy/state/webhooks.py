"""Atomic webhook receipt and one-time task admission."""

from __future__ import annotations

from dataclasses import replace

from aiosqlite import (  # noqa: TC002, RUF100 - beartype resolves state boundary annotations at runtime.
    Connection,
    Row,
)

from pynchy.conversation.api import ConversationDeliveryStatus
from pynchy.scheduling.api import (
    ScheduledTask,
    SessionPolicy,
)
from pynchy.state.connection import _get_db, atomic_write
from pynchy.state.conversation_controls import (
    _apply_conversation_control_state,
    _retire_conversation_for_terminal,
)
from pynchy.state.conversation_routing import _admit_conversation_delivery
from pynchy.state.webhook_effect_admission import admit_webhook_effect_delivery
from pynchy.state.webhook_models import (
    WebhookAdmission,
    WebhookConversationAdmission,
    WebhookConversationRequest,
    WebhookReceipt,
)
from pynchy.webhook_effects import (  # noqa: TC001, RUF100 - beartype resolves admission evidence.
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


def _row_to_task(row: Row | None) -> ScheduledTask | None:
    if row is None:
        return None
    return ScheduledTask(
        id=row["id"],
        group_folder=row["group_folder"],
        chat_jid=row["chat_jid"],
        prompt=row["prompt"],
        schedule_type=row["schedule_type"],
        schedule_value=row["schedule_value"],
        session_policy=SessionPolicy(row["session_policy"] or SessionPolicy.RESET_BEFORE_RUN),
        next_run=row["next_run"],
        last_run=row["last_run"],
        last_result=row["last_result"],
        status=row["status"],
        created_at=row["created_at"],
        repo_access=row["repo_access"] or None,
        input_source=row["input_source"] or "scheduled_task",
        config_job_name=row["config_job_name"] or None,
        config_job_is_deterministic=(
            None
            if row["config_job_is_deterministic"] is None
            else bool(row["config_job_is_deterministic"])
        ),
        config_job_command=row["config_job_command"] or None,
        config_job_cwd=row["config_job_cwd"] or None,
        config_job_timeout_seconds=row["config_job_timeout_seconds"],
        config_job_display_name=row["config_job_display_name"] or None,
        config_job_pre_run_command=row["config_job_pre_run_command"] or None,
        config_job_pre_run_cwd=row["config_job_pre_run_cwd"] or None,
        config_job_pre_run_timeout_seconds=row["config_job_pre_run_timeout_seconds"],
        derived_thread_name=row["derived_thread_name"] or None,
        bound_chat_jid=row["bound_chat_jid"] or None,
        bound_group_folder=row["bound_group_folder"] or None,
        conversation_id=row["conversation_id"] or None,
        last_reset_occurrence=row["last_reset_occurrence"] or None,
        occurrence_generation=row["occurrence_generation"],
        occurrence_due_at=row["occurrence_due_at"] or None,
        superseded_occurrence_generation=row["superseded_occurrence_generation"],
        superseded_occurrence_due_at=row["superseded_occurrence_due_at"] or None,
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
        task=_row_to_task(task_row),
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


async def _insert_task(database: Connection, task: ScheduledTask) -> None:
    await database.execute(
        """
        INSERT INTO scheduled_tasks
            (id, group_folder, chat_jid, prompt, schedule_type,
             schedule_value, session_policy, next_run, status, created_at,
             repo_access, input_source, config_job_name, config_job_is_deterministic,
             config_job_command, config_job_cwd, config_job_timeout_seconds,
             config_job_display_name, config_job_pre_run_command, config_job_pre_run_cwd,
             config_job_pre_run_timeout_seconds, derived_thread_name,
             bound_chat_jid, bound_group_folder, conversation_id, last_reset_occurrence,
             occurrence_generation, occurrence_due_at, superseded_occurrence_generation,
             superseded_occurrence_due_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?)
        """,
        (
            task.id,
            task.group_folder,
            task.chat_jid,
            task.prompt,
            task.schedule_type,
            task.schedule_value,
            task.session_policy,
            task.next_run,
            task.status,
            task.created_at,
            task.repo_access,
            task.input_source,
            task.config_job_name,
            task.config_job_is_deterministic,
            task.config_job_command,
            task.config_job_cwd,
            task.config_job_timeout_seconds,
            task.config_job_display_name,
            task.config_job_pre_run_command,
            task.config_job_pre_run_cwd,
            task.config_job_pre_run_timeout_seconds,
            task.derived_thread_name,
            task.bound_chat_jid,
            task.bound_group_folder,
            task.conversation_id,
            task.last_reset_occurrence,
            task.occurrence_generation,
            task.occurrence_due_at,
            task.superseded_occurrence_generation,
            task.superseded_occurrence_due_at,
        ),
    )


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
