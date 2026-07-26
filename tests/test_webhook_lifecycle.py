"""Business coverage for provider-neutral lifecycle-only webhook deliveries."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from aiohttp.test_utils import TestClient, TestServer
from linear_webhook_test_support import CursorDeps, LinearWebhookHarness, public_runtime

from pynchy.conversation.models import (
    ConversationClaimId,
    ConversationDeliveryStatus,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
)
from pynchy.host.orchestrator.http_server import create_http_app
from pynchy.host.orchestrator.messaging.cursor import complete_turn_with_cursor
from pynchy.host.orchestrator.webhook_conversations import WebhookConversationDispatcher
from pynchy.plugins.webhooks import (
    WebhookConversation,
    WebhookEvent,
    WebhookLifecycle,
    WebhookLifecycleDelivery,
    WebhookRoute,
)
from pynchy.state import (
    WebhookReceipt,
    admit_webhook_receipt,
    claim_next_conversation_delivery,
    get_conversation_control_binding,
    get_conversation_delivery,
    get_webhook_receipt,
    init_test_database,
    prepare_conversation_delivery_recovery,
)

_NOW = datetime(2026, 7, 26, tzinfo=UTC).isoformat()
_SUBJECT = ConversationSubject(
    namespace=ConversationSubjectNamespace("test-provider:tenant:issue"),
    key=ConversationSubjectKey("issue-1"),
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping


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


def _conversation(*, closed: bool | None = None) -> WebhookConversation:
    return WebhookConversation(
        subject=_SUBJECT,
        control_title="[TEST-1] Lifecycle delivery",
        control_closed=closed,
        workspace="project",
        public_source=False,
    )


def _message_event(delivery_id: str, *, closed: bool | None = False) -> WebhookEvent:
    return WebhookEvent(
        delivery_id=delivery_id,
        event_type="Issue",
        action="update",
        subject_id="issue-1",
        occurred_at=_NOW,
        instructions="Handle the routed provider update.",
        external_context={"delivery": delivery_id},
        conversation=_conversation(closed=closed),
    )


def _lifecycle_event(delivery_id: str) -> WebhookEvent:
    return WebhookEvent(
        delivery_id=delivery_id,
        event_type="Issue",
        action="update",
        subject_id="issue-1",
        occurred_at=_NOW,
        instructions=None,
        external_context=None,
        conversation=_conversation(closed=True),
        lifecycle=WebhookLifecycle(context={"state_id": "done-state"}),
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
    receipt = WebhookReceipt(
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
    admission = await admit_webhook_receipt(receipt, None)
    assert admission.created is True
    conversation_id = await dispatcher.admit(
        route,
        event,
        None if event.lifecycle is not None else "Routed webhook prompt",
    )
    assert conversation_id is not None
    return conversation_id


async def test_lifecycle_waits_for_older_turn_closes_once_and_wakes_its_sibling() -> None:
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    seen_lifecycles: list[WebhookLifecycleDelivery] = []
    binding_at_callback = []

    async def handle_lifecycle(delivery: WebhookLifecycleDelivery) -> None:
        binding = await get_conversation_control_binding(delivery.conversation_id)
        binding_at_callback.append(binding)
        seen_lifecycles.append(delivery)

    route = _route(handle_lifecycle)
    dispatcher = WebhookConversationDispatcher(deps=harness, routes=(route,))
    await dispatcher.start()
    try:
        first_event = _message_event("message-before-terminal")
        conversation_id = await _admit(dispatcher, route, first_event)
        await dispatcher.wake(conversation_id)
        first_message = harness.ingested[0]
        before_close = await get_conversation_control_binding(conversation_id)
        assert before_close is not None

        terminal_event = _lifecycle_event("terminal-state")
        await _admit(dispatcher, route, terminal_event)
        await dispatcher.wake(conversation_id)
        after_event = _message_event("message-after-terminal", closed=None)
        await _admit(dispatcher, route, after_event)

        assert len(harness.ingested) == 1
        assert not seen_lifecycles

        await complete_turn_with_cursor(
            CursorDeps(),
            first_message.chat_jid,
            first_message.timestamp,
            "turn-before-terminal",
            conversation_claim_id=first_message.metadata["conversation_claim_id"],
        )

        terminal_delivery = await get_conversation_delivery(
            next(
                delivery.identity
                for delivery in seen_lifecycles
                if delivery.identity.delivery_id == "terminal-state"
            )
        )
        after_close = await get_conversation_control_binding(conversation_id)
        assert terminal_delivery is not None
        assert terminal_delivery.status is ConversationDeliveryStatus.COMPLETED
        assert len(seen_lifecycles) == 1
        assert seen_lifecycles[0].subject_id == "issue-1"
        assert seen_lifecycles[0].context == {"state_id": "done-state"}
        assert len(binding_at_callback) == 1
        assert binding_at_callback[0] is not None
        assert binding_at_callback[0].closed is True
        assert after_close is not None
        assert after_close.closed is True
        assert after_close.parent_workspace == before_close.parent_workspace
        assert after_close.parent_jid == before_close.parent_jid
        assert after_close.thread_jid == before_close.thread_jid
        assert after_close.title == before_close.title
        assert [message.id for message in harness.ingested] == [
            "message-before-terminal",
            "message-after-terminal",
        ]

        await dispatcher.admit(route, terminal_event, None)
        await dispatcher.wake(conversation_id)
        assert len(seen_lifecycles) == 1
    finally:
        dispatcher.close()


async def test_lifecycle_delivery_does_not_create_a_message_or_runtime_workspace() -> None:
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    seen_lifecycles: list[WebhookLifecycleDelivery] = []

    async def handle_lifecycle(delivery: WebhookLifecycleDelivery) -> None:
        claimed = await get_conversation_delivery(delivery.identity)
        assert claimed is not None
        assert claimed.status is ConversationDeliveryStatus.CLAIMED
        seen_lifecycles.append(delivery)

    route = _route(handle_lifecycle)
    dispatcher = WebhookConversationDispatcher(deps=harness, routes=(route,))
    await dispatcher.start()
    try:
        conversation_id = await _admit(dispatcher, route, _lifecycle_event("terminal-alone"))
        await dispatcher.wake(conversation_id)

        assert len(seen_lifecycles) == 1
        assert not harness.ingested
        assert not harness.channel.created
        assert set(harness.workspace_map) == {harness.workspace.jid}
    finally:
        dispatcher.close()


async def test_lifecycle_ingress_records_a_lifecycle_receipt_without_an_agent_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    seen_lifecycles: list[WebhookLifecycleDelivery] = []

    async def handle_lifecycle(delivery: WebhookLifecycleDelivery) -> None:
        claimed = await get_conversation_delivery(delivery.identity)
        assert claimed is not None
        assert claimed.status is ConversationDeliveryStatus.CLAIMED
        seen_lifecycles.append(delivery)

    route = replace(_route(handle_lifecycle), parse=_lifecycle_parser)
    monkeypatch.setenv(route.secret_env, route.secret_env)
    app = create_http_app(harness, runtime=public_runtime(), webhook_routes=(route,))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            route.path,
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        body = await response.json()
        duplicate_response = await client.post(
            route.path,
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        duplicate = await duplicate_response.json()
    finally:
        await client.close()

    receipt = await get_webhook_receipt("test-provider", "project", "terminal-over-http")
    assert response.status == duplicate_response.status == 200
    assert body == {"status": "accepted", "duplicate": False}
    assert duplicate == {"status": "accepted", "duplicate": True}
    assert receipt is not None
    assert receipt.disposition == "lifecycle"
    assert len(seen_lifecycles) == 1
    assert not harness.ingested
    assert not harness.channel.created


async def test_lifecycle_callback_retry_keeps_the_original_control_close_transition() -> None:
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    close_update_times: list[str] = []

    async def handle_lifecycle(delivery: WebhookLifecycleDelivery) -> None:
        binding = await get_conversation_control_binding(delivery.conversation_id)
        assert binding is not None
        assert binding.closed is True
        close_update_times.append(binding.updated_at)
        if len(close_update_times) == 1:
            raise RuntimeError("retry the route-owned lifecycle effect")

    route = _route(handle_lifecycle)
    dispatcher = WebhookConversationDispatcher(deps=harness, routes=(route,))
    await dispatcher.start()
    try:
        conversation_id = await _admit(dispatcher, route, _message_event("message-before-retry"))
        await dispatcher.wake(conversation_id)
        first_message = harness.ingested[0]
        await _admit(dispatcher, route, _lifecycle_event("retry-after-close"))

        await complete_turn_with_cursor(
            CursorDeps(),
            first_message.chat_jid,
            first_message.timestamp,
            "turn-before-lifecycle-retry",
            conversation_claim_id=first_message.metadata["conversation_claim_id"],
        )
        await dispatcher.wake(conversation_id)

        assert len(close_update_times) == 2
        assert close_update_times[0] == close_update_times[1]
        assert len(harness.ingested) == 1
    finally:
        dispatcher.close()


async def test_recovery_retries_a_claimed_lifecycle_delivery_without_an_agent_turn() -> None:
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    seen_lifecycles: list[WebhookLifecycleDelivery] = []

    async def handle_lifecycle(delivery: WebhookLifecycleDelivery) -> None:
        claimed = await get_conversation_delivery(delivery.identity)
        assert claimed is not None
        assert claimed.status is ConversationDeliveryStatus.CLAIMED
        seen_lifecycles.append(delivery)

    route = _route(handle_lifecycle)
    original = WebhookConversationDispatcher(deps=harness, routes=(route,))
    await original.start()
    try:
        conversation_id = await _admit(
            dispatcher=original,
            route=route,
            event=_lifecycle_event("retry"),
        )
        claimed = await claim_next_conversation_delivery(
            conversation_id,
            ConversationClaimId("lifecycle-claim-before-restart"),
        )
        assert claimed is not None
        await prepare_conversation_delivery_recovery()
    finally:
        original.close()

    restarted = WebhookConversationDispatcher(deps=harness, routes=(route,))
    await restarted.start()
    try:
        delivery = await get_conversation_delivery(claimed.identity)
        assert delivery is not None
        assert delivery.status is ConversationDeliveryStatus.COMPLETED
        assert len(seen_lifecycles) == 1
        assert not harness.ingested
        assert not harness.channel.created
    finally:
        restarted.close()
