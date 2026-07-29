"""HTTP ingress coverage for callback-first Linear comment correlation."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

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
    webhook_route,
)

from pynchy.conversation.models import (
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.host.orchestrator.http_server import create_http_app
from pynchy.plugins.integrations.linear_self_echoes import linear_self_echo_recorder
from pynchy.plugins.integrations.linear_webhook_evidence import comment_webhook_evidence
from pynchy.state import (
    classify_webhook_effect_callback,
    get_conversation_delivery,
    init_test_database,
)
from pynchy.webhook_effects import WebhookEffectCallbackDecision, WebhookEffectScope

_COMMENT_ID = "comment-1"
_ISSUE_ID = "issue-1"
_REVISION = "2026-07-26T16:00:01+00:00"


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


def _comment_payload(now: datetime, *, revision: str = _REVISION) -> dict[str, object]:
    return payload(
        now=now,
        data={
            "id": _COMMENT_ID,
            "issueId": _ISSUE_ID,
            "body": "Pynchy posted this update.",
            "updatedAt": revision,
        },
    )


def _evidence(*, revision: str = _REVISION):
    return comment_webhook_evidence(
        route_config().tool,
        comment_id=_COMMENT_ID,
        issue_id=_ISSUE_ID,
        revision=revision,
    )


async def _start_effect():
    recorder = linear_self_echo_recorder(route_config().tool)
    scope = WebhookEffectScope(
        provider="linear",
        account=route_config().tool,
        event_type="Comment",
        event_action="create",
        subject_id=_ISSUE_ID,
    )
    effect_id = await recorder.begin(scope)
    await recorder.mark_executing(effect_id)
    return recorder, effect_id


async def test_callback_before_response_is_acknowledged_but_never_wakes_an_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", SIGNING_KEY)
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    recorder, effect_id = await _start_effect()
    processed = AsyncMock(side_effect=lambda event: event)
    route = replace(webhook_route(), process_event=processed)
    app = create_http_app(harness, runtime=public_runtime(), webhook_routes=(route,))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        status, body = await post_linear_event(client, _comment_payload(datetime.now(UTC)))
        assert status == 200
        assert body == {"status": "accepted", "duplicate": False}
        assert harness.ingested == []

        await recorder.confirm(effect_id, _evidence())
        assert (
            await classify_webhook_effect_callback(_evidence(), datetime.now(UTC).isoformat())
            is WebhookEffectCallbackDecision.SUPPRESSED
        )
    finally:
        await client.close()

    assert harness.ingested == []
    processed.assert_not_awaited()
    delivery = await get_conversation_delivery(webhook_route_identity("project", DELIVERY_ID))
    assert delivery is not None
    assert delivery.status.value == "completed"


async def test_nonmatching_callback_releases_and_wakes_after_response_disproves_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", SIGNING_KEY)
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    recorder, effect_id = await _start_effect()
    processed = AsyncMock(side_effect=lambda event: event)
    route = replace(webhook_route(), process_event=processed)
    app = create_http_app(harness, runtime=public_runtime(), webhook_routes=(route,))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        status, body = await post_linear_event(
            client,
            _comment_payload(
                datetime.now(UTC),
                revision="2026-07-26T16:00:09+00:00",
            ),
        )
        assert status == 200
        assert body == {"status": "accepted", "duplicate": False}
        assert harness.ingested == []
        processed.assert_not_awaited()

        await recorder.confirm(effect_id, _evidence())
        assert len(harness.ingested) == 1
        assert "Pynchy posted this update." in harness.ingested[0].content
        processed.assert_awaited_once()
    finally:
        await client.close()


def webhook_route_identity(route: str, delivery_id: str) -> ExternalDeliveryIdentity:
    """Build the provider-neutral identity used by the durable FIFO."""
    return ExternalDeliveryIdentity(
        provider=ExternalProvider("linear"),
        route=ExternalRoute(route),
        delivery_id=ExternalDeliveryId(delivery_id),
    )
