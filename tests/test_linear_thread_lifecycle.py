"""Behavioral coverage for Linear-owned Discord thread lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from linear_webhook_test_support import (
    SECOND_DELIVERY_ID,
    SIGNING_KEY,
    THIRD_DELIVERY_ID,
    CursorDeps,
    LinearWebhookHarness,
    payload,
    post_linear_event,
    public_runtime,
    webhook_route,
)

from pynchy.host.orchestrator.http_server import create_http_app
from pynchy.host.orchestrator.messaging.cursor import complete_turn_with_cursor
from pynchy.state import (
    complete_in_flight_turn,
    get_conversation_control_binding,
    init_test_database,
)

if TYPE_CHECKING:
    from pynchy.types import NewMessage


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


def _issue_payload(now: datetime, state: str) -> dict[str, Any]:
    return payload(
        now=now,
        event_type="Issue",
        action="update",
        data={
            "id": "issue-1",
            "identifier": "PYN-1",
            "title": "Webhook callbacks",
            "state": {"id": f"state-{state.casefold()}", "name": state},
        },
        updated_from={"stateId": "previous-state"},
    )


async def _complete(message: NewMessage, turn_id: str) -> None:
    await complete_turn_with_cursor(
        CursorDeps(),
        message.chat_jid,
        message.timestamp,
        turn_id,
        conversation_claim_id=message.metadata["conversation_claim_id"],
    )


async def test_done_closes_comment_preserves_and_non_done_reopens_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", SIGNING_KEY)
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    app = create_http_app(harness, runtime=public_runtime(), webhook_routes=(webhook_route(),))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        now = datetime.now(UTC)
        await post_linear_event(client, _issue_payload(now, "Done"))
        done_message = harness.ingested[0]
        binding = await get_conversation_control_binding(done_message.metadata["conversation_id"])
        assert binding is not None
        assert binding.closed is True
        assert harness.channel.closed[done_message.chat_jid] is False

        await _complete(done_message, "linear-done-turn")
        assert harness.channel.closed[done_message.chat_jid] is True

        await post_linear_event(
            client,
            payload(
                now=now + timedelta(seconds=1),
                data={"id": "comment-2", "issueId": "issue-1", "body": "follow-up"},
            ),
            delivery_id=SECOND_DELIVERY_ID,
        )
        comment_message = harness.ingested[1]
        comment_binding = await get_conversation_control_binding(
            comment_message.metadata["conversation_id"]
        )
        assert comment_binding is not None
        assert comment_binding.closed is True
        assert harness.channel.closed[comment_message.chat_jid] is False

        await _complete(comment_message, "linear-done-comment-turn")
        assert harness.channel.closed[comment_message.chat_jid] is True

        await post_linear_event(
            client,
            _issue_payload(now + timedelta(seconds=2), "In Progress"),
            delivery_id=THIRD_DELIVERY_ID,
        )
        reopened_message = harness.ingested[2]
        reopened_binding = await get_conversation_control_binding(
            reopened_message.metadata["conversation_id"]
        )
        assert reopened_binding is not None
        assert reopened_binding.closed is False
        assert harness.channel.closed[reopened_message.chat_jid] is False

        await _complete(reopened_message, "linear-reopened-turn")
        assert harness.channel.closed[reopened_message.chat_jid] is False
    finally:
        await client.close()


async def test_startup_reconciles_completed_done_delivery_after_callback_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", SIGNING_KEY)
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    app = create_http_app(harness, runtime=public_runtime(), webhook_routes=(webhook_route(),))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await post_linear_event(client, _issue_payload(datetime.now(UTC), "Done"))
        message = harness.ingested[0]
        await complete_in_flight_turn(
            "linear-done-without-callback",
            conversation_claim_id=message.metadata["conversation_claim_id"],
        )
        assert harness.channel.closed[message.chat_jid] is False
    finally:
        await client.close()

    restarted = create_http_app(
        harness,
        runtime=public_runtime(),
        webhook_routes=(webhook_route(),),
    )
    restarted_client = TestClient(TestServer(restarted))
    await restarted_client.start_server()
    try:
        assert harness.channel.closed[message.chat_jid] is True
    finally:
        await restarted_client.close()
