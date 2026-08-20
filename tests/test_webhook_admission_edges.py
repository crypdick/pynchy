"""Input and idempotency contracts for webhook admission."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.conversation.api import (
    ConversationClaimId,
    ConversationDelivery,
    ConversationDeliveryStatus,
    ConversationId,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.host.orchestrator.webhook_conversation_admission import (
    conversation_admission_request,
    process_deferred_event,
)
from pynchy.host.orchestrator.webhook_event_payloads import webhook_event_payload
from pynchy.identifiers import GroupFolder
from pynchy.plugins.api import WebhookActor
from pynchy.plugins.integrations.linear_webhook_evidence import comment_webhook_evidence
from pynchy.scheduling.api import ScheduledTask, SessionPolicy
from pynchy.state import (
    WebhookConversationRequest,
    WebhookReceipt,
    admit_webhook_conversation,
    admit_webhook_receipt,
    init_test_database,
)
from tests.webhook_lifecycle_support import _lifecycle_event, _message_event, _route

pytest_plugins = ("tests.state_support",)


def _delivery(payload: dict[str, object]) -> ConversationDelivery:
    return ConversationDelivery(
        sequence=1,
        identity=ExternalDeliveryIdentity(
            provider=ExternalProvider("test-provider"),
            route=ExternalRoute("project"),
            delivery_id=ExternalDeliveryId("delivery-1"),
        ),
        conversation_id=ConversationId("conversation-1"),
        status=ConversationDeliveryStatus.HELD,
        received_at="2026-07-29T00:00:00Z",
        payload=payload,
    )


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


def test_conversation_admission_request_rejects_missing_routing_contracts() -> None:
    event = _message_event("delivery-admission")
    assert (
        conversation_admission_request(_route(), replace(event, conversation=None), "prompt")
        is None
    )

    conversation = replace(event.conversation, workspace=None)
    assert conversation is not None
    with pytest.raises(RuntimeError, match="no workspace owner"):
        conversation_admission_request(
            replace(_route(), workspace=None),
            replace(event, conversation=conversation),
            "prompt",
        )

    with pytest.raises(ValueError, match="no prompt"):
        conversation_admission_request(_route(), event, None)


def test_conversation_admission_records_human_actor_provenance() -> None:
    event = replace(
        _message_event("delivery-human"),
        actor=WebhookActor(id="user-1", kind="User"),
    )

    request = conversation_admission_request(_route(), event, "prompt")

    assert request is not None
    assert request.payload["human_derived"] is True


@pytest.mark.asyncio
async def test_process_deferred_event_rejects_invalid_payload_and_processor_contracts() -> None:
    claim_id = ConversationClaimId("claim-1")
    route = _route()
    with pytest.raises(TypeError, match="not an object"):
        await process_deferred_event(
            _delivery({"deferred_process_event": "invalid"}), claim_id, route
        )

    event = _message_event("delivery-deferred")
    payload = {"deferred_process_event": webhook_event_payload(event)}
    with pytest.raises(RuntimeError, match="lost its trusted processor"):
        await process_deferred_event(_delivery(payload), claim_id, route)

    unroutable = replace(event, conversation=None)
    route = replace(route, process_event=AsyncMock(return_value=unroutable))
    with pytest.raises(TypeError, match="unroutable"):
        await process_deferred_event(_delivery(payload), claim_id, route)

    lifecycle = _lifecycle_event("delivery-deferred")
    route = replace(route, process_event=AsyncMock(return_value=lifecycle))
    with pytest.raises(TypeError, match="became a lifecycle"):
        await process_deferred_event(_delivery(payload), claim_id, route)
