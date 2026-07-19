"""Atomic webhook receipt and one-time task admission."""

from __future__ import annotations

from aiosqlite import (  # noqa: TC002, RUF100 - beartype resolves state boundary annotations at runtime.
    Connection,
    Row,
)

from pynchy.state.connection import _get_db, atomic_write
from pynchy.state.webhook_models import WebhookAdmission, WebhookReceipt
from pynchy.types import ScheduledTask


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
        context_mode=row["context_mode"] or "isolated",
        next_run=row["next_run"],
        last_run=row["last_run"],
        last_result=row["last_result"],
        status=row["status"],
        created_at=row["created_at"],
        repo_access=row["repo_access"] or None,
        input_source=row["input_source"] or "scheduled_task",
        config_job_name=row["config_job_name"] or None,
    )


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
    task_row = None
    if existing.task_id is not None:
        task_cursor = await database.execute(
            "SELECT * FROM scheduled_tasks WHERE id = ?",
            (existing.task_id,),
        )
        task_row = await task_cursor.fetchone()
    return WebhookAdmission(receipt=existing, task=_row_to_task(task_row), created=False)


async def _insert_task(database: Connection, task: ScheduledTask) -> None:
    await database.execute(
        """
        INSERT INTO scheduled_tasks
            (id, group_folder, chat_jid, prompt, schedule_type,
             schedule_value, context_mode, next_run, status, created_at,
             repo_access, input_source, config_job_name)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task.id,
            task.group_folder,
            task.chat_jid,
            task.prompt,
            task.schedule_type,
            task.schedule_value,
            task.context_mode,
            task.next_run,
            task.status,
            task.created_at,
            task.repo_access,
            task.input_source,
            task.config_job_name,
        ),
    )


async def admit_webhook_receipt(
    receipt: WebhookReceipt,
    task: ScheduledTask | None,
) -> WebhookAdmission:
    """Persist one receipt and its task atomically, deduplicated by delivery ID."""
    if receipt.task_id != (task.id if task is not None else None):
        raise ValueError("Webhook receipt task identity does not match its admitted task")

    async with atomic_write() as database:
        existing = await _existing_admission(database, receipt)
        if existing is not None:
            return existing
        if task is not None:
            await _insert_task(database, task)
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
    return WebhookAdmission(receipt=receipt, task=task, created=True)


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
