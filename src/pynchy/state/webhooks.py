"""Atomic webhook receipt and one-time task admission."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from aiosqlite import (  # noqa: TC002, RUF100 - beartype resolves state boundary annotations at runtime.
    Connection,
    Row,
)

from pynchy.state.connection import _get_db, atomic_write
from pynchy.state.webhook_models import (
    LinearCommentSelfEcho,
    LinearIssueStateSelfEcho,
    WebhookAdmission,
    WebhookReceipt,
)
from pynchy.types import ScheduledTask, SessionPolicy

_LINEAR_COMMENT_SELF_ECHO_REASON = "pynchy_self_comment_echo"
_LINEAR_ISSUE_STATE_SELF_ECHO_REASON = "pynchy_self_issue_state_echo"
_LINEAR_SELF_ECHO_REASONS = frozenset(
    {_LINEAR_COMMENT_SELF_ECHO_REASON, _LINEAR_ISSUE_STATE_SELF_ECHO_REASON}
)

type _LinearSelfEcho = LinearCommentSelfEcho | LinearIssueStateSelfEcho


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
    return WebhookAdmission(
        receipt=existing,
        task=_row_to_task(task_row),
        created=False,
        self_echo_suppressed=existing.ignored_reason in _LINEAR_SELF_ECHO_REASONS,
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
             repo_access, input_source, config_job_name, derived_thread_name,
             bound_chat_jid, bound_group_folder, conversation_id, last_reset_occurrence,
             occurrence_generation, occurrence_due_at, superseded_occurrence_generation,
             superseded_occurrence_due_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


def _validate_linear_comment_self_echo(
    receipt: WebhookReceipt,
    self_echo: LinearCommentSelfEcho,
) -> None:
    if receipt.provider != "linear" or receipt.event_type != "Comment":
        raise ValueError("Linear comment self-echo markers require a Linear Comment receipt")
    if receipt.event_action != self_echo.action:
        raise ValueError("Linear comment self-echo action does not match its receipt")
    if receipt.subject_id != self_echo.issue_id:
        raise ValueError("Linear comment self-echo issue does not match its receipt")


async def _consume_linear_comment_self_echo(
    database: Connection,
    self_echo: LinearCommentSelfEcho,
) -> bool:
    cursor = await database.execute(
        """
        DELETE FROM linear_comment_self_echoes
        WHERE account_name = ?
          AND comment_id = ?
          AND issue_id = ?
          AND revision = ?
          AND action = ?
        """,
        (
            self_echo.account_name,
            self_echo.comment_id,
            self_echo.issue_id,
            self_echo.revision,
            self_echo.action,
        ),
    )
    return cursor.rowcount == 1


def _validate_linear_issue_state_self_echo(
    receipt: WebhookReceipt,
    self_echo: LinearIssueStateSelfEcho,
) -> None:
    if receipt.provider != "linear" or receipt.event_type != "Issue":
        raise ValueError("Linear issue state self-echo markers require a Linear Issue receipt")
    if receipt.event_action != self_echo.action:
        raise ValueError("Linear issue state self-echo action does not match its receipt")
    if receipt.subject_id != self_echo.issue_id:
        raise ValueError("Linear issue state self-echo issue does not match its receipt")


async def _consume_linear_issue_state_self_echo(
    database: Connection,
    self_echo: LinearIssueStateSelfEcho,
) -> bool:
    cursor = await database.execute(
        """
        DELETE FROM linear_issue_state_self_echoes
        WHERE account_name = ?
          AND issue_id = ?
          AND state_id = ?
          AND revision = ?
          AND action = ?
        """,
        (
            self_echo.account_name,
            self_echo.issue_id,
            self_echo.state_id,
            self_echo.revision,
            self_echo.action,
        ),
    )
    return cursor.rowcount == 1


def _validate_linear_self_echo(receipt: WebhookReceipt, self_echo: _LinearSelfEcho) -> None:
    if isinstance(self_echo, LinearCommentSelfEcho):
        _validate_linear_comment_self_echo(receipt, self_echo)
        return
    _validate_linear_issue_state_self_echo(receipt, self_echo)


async def _consume_linear_self_echo(
    database: Connection,
    receipt: WebhookReceipt,
    self_echo: _LinearSelfEcho,
) -> bool:
    if isinstance(self_echo, LinearCommentSelfEcho):
        return await _consume_linear_comment_self_echo(database, self_echo)
    if receipt.disposition == "lifecycle":
        # Terminal Issue callbacks close the conversation and apply their
        # lifecycle effect even when a matching self-echo marker exists.
        return False
    return await _consume_linear_issue_state_self_echo(database, self_echo)


def _linear_self_echo_reason(self_echo: _LinearSelfEcho) -> str:
    if isinstance(self_echo, LinearCommentSelfEcho):
        return _LINEAR_COMMENT_SELF_ECHO_REASON
    return _LINEAR_ISSUE_STATE_SELF_ECHO_REASON


async def record_linear_comment_self_echo(
    self_echo: LinearCommentSelfEcho,
) -> None:
    """Record exact provider evidence before its authenticated callback arrives."""
    async with atomic_write() as database:
        await database.execute(
            """
            INSERT OR IGNORE INTO linear_comment_self_echoes (
                account_name, comment_id, issue_id, revision, action, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                self_echo.account_name,
                self_echo.comment_id,
                self_echo.issue_id,
                self_echo.revision,
                self_echo.action,
                datetime.now(UTC).isoformat(),
            ),
        )


async def record_linear_issue_state_self_echo(
    self_echo: LinearIssueStateSelfEcho,
) -> None:
    """Record exact provider evidence before its authenticated callback arrives."""
    async with atomic_write() as database:
        await database.execute(
            """
            INSERT OR IGNORE INTO linear_issue_state_self_echoes (
                account_name, issue_id, state_id, revision, action, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                self_echo.account_name,
                self_echo.issue_id,
                self_echo.state_id,
                self_echo.revision,
                self_echo.action,
                datetime.now(UTC).isoformat(),
            ),
        )


async def admit_webhook_receipt(
    receipt: WebhookReceipt,
    task: ScheduledTask | None,
    *,
    self_echo: _LinearSelfEcho | None = None,
) -> WebhookAdmission:
    """Persist one receipt and its task, consuming a matching self echo atomically."""
    if receipt.task_id != (task.id if task is not None else None):
        raise ValueError("Webhook receipt task identity does not match its admitted task")
    if self_echo is not None:
        _validate_linear_self_echo(receipt, self_echo)

    async with atomic_write() as database:
        await _ensure_external_receipt(database, receipt)
        existing = await _existing_admission(database, receipt)
        if existing is not None:
            return existing
        self_echo_suppressed = self_echo is not None and await _consume_linear_self_echo(
            database,
            receipt,
            self_echo,
        )
        admitted_receipt = receipt
        admitted_task = task
        if self_echo_suppressed and self_echo is not None:
            admitted_receipt = replace(
                receipt,
                disposition="ignored",
                ignored_reason=_linear_self_echo_reason(self_echo),
                task_id=None,
            )
            admitted_task = None
        if admitted_task is not None:
            await _insert_task(database, admitted_task)
        await database.execute(
            """
            INSERT INTO webhook_receipts (
                provider, route, delivery_id, workspace, event_type, event_action,
                subject_id, payload_sha256, disposition, ignored_reason, task_id,
                occurred_at, received_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                admitted_receipt.provider,
                admitted_receipt.route,
                admitted_receipt.delivery_id,
                admitted_receipt.workspace,
                admitted_receipt.event_type,
                admitted_receipt.event_action,
                admitted_receipt.subject_id,
                admitted_receipt.payload_sha256,
                admitted_receipt.disposition,
                admitted_receipt.ignored_reason,
                admitted_receipt.task_id,
                admitted_receipt.occurred_at,
                admitted_receipt.received_at,
            ),
        )
    return WebhookAdmission(
        receipt=admitted_receipt,
        task=admitted_task,
        created=True,
        self_echo_suppressed=self_echo_suppressed,
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
