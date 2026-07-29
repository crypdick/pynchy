"""Input and idempotency contracts for webhook admission."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.conversation.api import (
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.identifiers import GroupFolder
from pynchy.plugins.integrations.linear_webhook_evidence import comment_webhook_evidence
from pynchy.scheduling.api import ScheduledTask, SessionPolicy
from pynchy.state import (
    WebhookConversationRequest,
    WebhookReceipt,
    admit_webhook_conversation,
    admit_webhook_receipt,
    init_test_database,
)

pytest_plugins = ("tests.state_support",)


def _receipt(
    delivery_id: str = "delivery-1",
    *,
    task_id: str | None = None,
    disposition: str = "routed",
) -> WebhookReceipt:
    occurred_at = datetime.now(UTC).isoformat()
    return WebhookReceipt(
        provider="linear",
        route="project",
        delivery_id=delivery_id,
        workspace="project",
        event_type="Comment",
        event_action="create",
        subject_id="issue-1",
        payload_sha256=f"payload-{delivery_id}",
        disposition=disposition,  # type: ignore[arg-type]
        ignored_reason=None,
        task_id=task_id,
        occurred_at=occurred_at,
        received_at=occurred_at,
    )


def _task() -> ScheduledTask:
    return ScheduledTask(
        id="task-1",
        group_folder="project",
        chat_jid="project@g.us",
        prompt="Run the task",
        schedule_type="once",
        schedule_value="2026-07-29T00:00:00+00:00",
        session_policy=SessionPolicy.CONTINUE,
    )


def _request() -> WebhookConversationRequest:
    return WebhookConversationRequest(
        identity=ExternalDeliveryIdentity(
            provider=ExternalProvider("linear"),
            route=ExternalRoute("project"),
            delivery_id=ExternalDeliveryId("delivery-conversation"),
        ),
        subject=ConversationSubject(
            namespace=ConversationSubjectNamespace("linear:org:issue"),
            key=ConversationSubjectKey("issue-1"),
        ),
        workspace=GroupFolder("project"),
        payload={"content": "hello"},
    )


async def test_conflicting_duplicate_receipt_evidence_is_rejected() -> None:
    await init_test_database()
    first = _receipt()
    await admit_webhook_receipt(first, None)

    with (
        patch("pynchy.state.webhooks._ensure_external_receipt", new=AsyncMock()),
        pytest.raises(ValueError, match="conflicting receipt evidence"),
    ):
        await admit_webhook_receipt(replace(first, payload_sha256="different"), None)


async def test_existing_admission_reconstructs_its_scheduled_task() -> None:
    await init_test_database()
    task = _task()
    receipt = _receipt(task_id=task.id, disposition="accepted")
    await admit_webhook_receipt(receipt, task)

    admission = await admit_webhook_receipt(receipt, task)

    assert admission.created is False
    assert admission.task is not None
    assert admission.task.id == task.id


async def test_webhook_task_identity_and_effect_pairings_are_rejected() -> None:
    await init_test_database()
    task = _task()
    mismatched = _receipt(task_id="other-task", disposition="accepted")
    with pytest.raises(ValueError, match="task identity"):
        await admit_webhook_receipt(mismatched, task)

    with pytest.raises(ValueError, match="cannot create isolated tasks"):
        await admit_webhook_receipt(
            _receipt(task_id=task.id, disposition="accepted"),
            task,
            effect_evidence=comment_webhook_evidence(
                "project",
                comment_id="comment-1",
                issue_id="issue-1",
                revision="2026-07-29T00:00:00+00:00",
            ),
        )


async def test_conversation_admission_requires_a_routed_or_lifecycle_receipt() -> None:
    await init_test_database()

    with pytest.raises(ValueError, match="requires a routed receipt"):
        await admit_webhook_conversation(
            _receipt(disposition="accepted", task_id="task-1"),
            _request(),
        )
