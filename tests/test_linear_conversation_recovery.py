"""Recovery coverage for Linear webhook issue conversations."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from aiohttp.test_utils import TestClient, TestServer
from linear_webhook_test_support import (
    DELIVERY_ID,
    SIGNING_KEY,
    LinearWebhookHarness,
    payload,
    post_linear_event,
    public_runtime,
    route_config,
    signed_request,
    webhook_route,
)

from pynchy.conversation.models import (
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.host.orchestrator.http_server import create_http_app
from pynchy.plugins.integrations.linear_webhooks import parse_linear_webhook
from pynchy.state import (
    WebhookReceipt,
    admit_conversation_delivery,
    admit_webhook_receipt,
    init_test_database,
)
from pynchy.types import GroupFolder


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


def _receipt(raw_body: bytes, now: datetime) -> WebhookReceipt:
    return WebhookReceipt(
        provider="linear",
        route="project",
        delivery_id=DELIVERY_ID,
        workspace="project",
        event_type="Comment",
        event_action="create",
        subject_id="issue-1",
        payload_sha256=hashlib.sha256(raw_body).hexdigest(),
        disposition="routed",
        ignored_reason=None,
        task_id=None,
        occurred_at=now.isoformat(),
        received_at=now.isoformat(),
    )


async def test_provider_replay_repairs_receipt_without_conversation_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", SIGNING_KEY)
    now = datetime.now(UTC)
    body = payload(now=now)
    raw_body, _headers = signed_request(body)
    await admit_webhook_receipt(_receipt(raw_body, now), None)
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    app = create_http_app(
        harness,
        runtime=public_runtime(),
        webhook_routes=(webhook_route(),),
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        status, response = await post_linear_event(client, body)
    finally:
        await client.close()

    assert status == 200
    assert response == {"status": "accepted", "duplicate": True}
    assert len(harness.ingested) == 1
    assert harness.ingested[0].metadata["conversation_id"]


async def test_http_startup_wakes_pending_linear_conversation_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", SIGNING_KEY)
    now = datetime.now(UTC)
    body = payload(now=now)
    raw_body, headers = signed_request(body)
    event = parse_linear_webhook(
        raw_body,
        headers,
        SIGNING_KEY,
        now,
        config=route_config(),
    )
    assert event.conversation is not None
    await admit_webhook_receipt(_receipt(raw_body, now), None)
    admission = await admit_conversation_delivery(
        ExternalDeliveryIdentity(
            provider=ExternalProvider("linear"),
            route=ExternalRoute("project"),
            delivery_id=ExternalDeliveryId(DELIVERY_ID),
        ),
        event.conversation.subject,
        GroupFolder("project"),
        payload={
            "prompt": "Inspect current Linear state.\n\n<EXTERNAL_UNTRUSTED_CONTENT />",
            "control_title": event.conversation.control_title,
            "event_type": event.event_type,
            "event_action": event.action,
        },
    )
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    app = create_http_app(
        harness,
        runtime=public_runtime(),
        webhook_routes=(webhook_route(),),
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        assert len(harness.ingested) == 1
    finally:
        await client.close()

    message = harness.ingested[0]
    assert message.metadata["conversation_id"] == admission.conversation.id
    assert message.metadata["conversation_claim_id"]
