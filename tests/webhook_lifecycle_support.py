"""Business coverage for provider-neutral lifecycle-only webhook deliveries."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from pynchy.conversation.models import (
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.plugins.api import (
    WebhookConversation,
    WebhookEvent,
    WebhookLifecycle,
    WebhookLifecycleDelivery,
    WebhookRoute,
)
from pynchy.state import (
    WebhookReceipt,
    init_test_database,
)

_NOW = datetime(2026, 7, 26, tzinfo=UTC).isoformat()
_SUBJECT = ConversationSubject(
    namespace=ConversationSubjectNamespace("test-provider:tenant:issue"),
    key=ConversationSubjectKey("issue-1"),
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from pynchy.host.orchestrator.webhook_conversations import WebhookConversationDispatcher


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


def _unreachable_parser(
    raw_body: bytes,
    headers: Mapping[str, str],
    secret: str,
    now: datetime,
) -> WebhookEvent:
    del raw_body, headers, secret, now
    raise AssertionError("The dispatcher tests do not parse HTTP requests")


def _route(
    lifecycle_handler: Callable[[WebhookLifecycleDelivery], Awaitable[None]] | None = None,
) -> WebhookRoute:
    return WebhookRoute(
        provider="test-provider",
        name="project",
        workspace="project",
        secret_env=WebhookRoute.__name__.upper(),
        parse=_unreachable_parser,
        public_source=False,
        routes_conversations=True,
        process_lifecycle=lifecycle_handler,
    )


def _conversation(
    *,
    closed: bool | None = None,
    revision: str | None = None,
) -> WebhookConversation:
    return WebhookConversation(
        subject=_SUBJECT,
        control_title="[TEST-1] Lifecycle delivery",
        control_closed=closed,
        control_state_revision=revision,
        workspace="project",
        public_source=False,
    )


def _message_event(
    delivery_id: str,
    *,
    closed: bool | None = False,
    revision: str | None = None,
) -> WebhookEvent:
    return WebhookEvent(
        delivery_id=delivery_id,
        event_type="Issue",
        action="update",
        subject_id="issue-1",
        occurred_at=_NOW,
        instructions="Handle the routed provider update.",
        external_context={"delivery": delivery_id},
        conversation=_conversation(closed=closed, revision=revision),
    )


def _lifecycle_event(delivery_id: str, *, revision: str | None = None) -> WebhookEvent:
    return WebhookEvent(
        delivery_id=delivery_id,
        event_type="Issue",
        action="update",
        subject_id="issue-1",
        occurred_at=_NOW,
        instructions=None,
        external_context=None,
        conversation=_conversation(closed=True, revision=revision),
        lifecycle=WebhookLifecycle(context={"state_id": "done-state"}),
    )


def _delivery_identity(route: WebhookRoute, delivery_id: str) -> ExternalDeliveryIdentity:
    return ExternalDeliveryIdentity(
        provider=ExternalProvider(route.provider),
        route=ExternalRoute(route.name),
        delivery_id=ExternalDeliveryId(delivery_id),
    )


def _lifecycle_parser(
    raw_body: bytes,
    headers: Mapping[str, str],
    secret: str,
    now: datetime,
) -> WebhookEvent:
    del raw_body, headers, secret, now
    return _lifecycle_event("terminal-over-http")


async def _admit(
    dispatcher: WebhookConversationDispatcher,
    route: WebhookRoute,
    event: WebhookEvent,
) -> str:
    receipt = _receipt(route, event)
    admission, conversation_id = await dispatcher.admit_webhook(
        route,
        event,
        None if event.lifecycle is not None else "Routed webhook prompt",
        receipt,
        defer_process_event=False,
    )
    assert admission.created is True
    assert conversation_id is not None
    return conversation_id


def _receipt(route: WebhookRoute, event: WebhookEvent) -> WebhookReceipt:
    return WebhookReceipt(
        provider=route.provider,
        route=route.name,
        delivery_id=event.delivery_id,
        workspace="project",
        event_type=event.event_type,
        event_action=event.action,
        subject_id=event.subject_id,
        payload_sha256=f"sha-{event.delivery_id}",
        disposition="lifecycle" if event.lifecycle is not None else "routed",
        ignored_reason=None,
        task_id=None,
        occurred_at=event.occurred_at,
        received_at=_NOW,
    )
