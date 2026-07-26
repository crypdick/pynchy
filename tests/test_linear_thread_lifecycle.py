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

from pynchy.conversation.workspaces import routed_conversation_folder
from pynchy.host.orchestrator.http_server import create_http_app
from pynchy.host.orchestrator.messaging.cursor import complete_turn_with_cursor
from pynchy.host.orchestrator.webhook_ingress import recover_webhook_conversations
from pynchy.state import (
    get_conversation_control_binding,
    get_workspace_profile,
    init_test_database,
)

if TYPE_CHECKING:
    from pynchy.types import NewMessage


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


def _issue_payload(now: datetime, state: str) -> dict[str, Any]:
    state_type = "completed" if state == "Done" else "started"
    return payload(
        now=now,
        event_type="Issue",
        action="update",
        data={
            "id": "issue-1",
            "identifier": "PYN-1",
            "title": "Webhook callbacks",
            "state": {
                "id": f"state-{state.casefold()}",
                "name": state,
                "type": state_type,
            },
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


async def test_terminal_callback_closes_existing_control_without_an_agent_turn(
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
        await post_linear_event(client, payload(now=now))
        first_message = harness.ingested[0]
        conversation_id = first_message.metadata["conversation_id"]
        await _complete(first_message, "linear-first-turn")
        binding = await get_conversation_control_binding(conversation_id)
        assert binding is not None
        assert binding.closed is False

        await post_linear_event(
            client,
            _issue_payload(now + timedelta(seconds=1), "Done"),
            delivery_id=SECOND_DELIVERY_ID,
        )
        closed = await get_conversation_control_binding(conversation_id)
        assert closed is not None
        assert closed.closed is True
        assert closed.thread_jid == binding.thread_jid
        assert harness.channel.closed[closed.thread_jid] is True
        assert len(harness.ingested) == 1
        assert len(harness.channel.created) == 1

        await post_linear_event(
            client,
            _issue_payload(now + timedelta(seconds=2), "In Progress"),
            delivery_id=THIRD_DELIVERY_ID,
        )
        reopened_message = harness.ingested[1]
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


async def test_terminal_first_callback_creates_no_discord_thread_or_agent_turn(
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
        assert harness.ingested == []
        assert harness.channel.created == []
        assert set(harness.workspace_map) == {harness.workspace.jid}
    finally:
        await client.close()


async def test_startup_restores_closed_existing_control_without_waking_an_agent(
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
        await post_linear_event(client, payload(now=now))
        message = harness.ingested[0]
        await _complete(message, "linear-normal-turn")
        await post_linear_event(
            client,
            _issue_payload(now + timedelta(seconds=1), "Done"),
            delivery_id=SECOND_DELIVERY_ID,
        )
        binding = await get_conversation_control_binding(message.metadata["conversation_id"])
        assert binding is not None
        assert binding.closed is True
        assert harness.channel.closed[binding.thread_jid] is True
        # Mimic a provider-side control change while the HTTP runtime is down.
        harness.channel.closed[binding.thread_jid] = False
    finally:
        await client.close()

    restarted_harness = LinearWebhookHarness()
    restarted_harness.channel = harness.channel
    restarted = create_http_app(
        restarted_harness,
        runtime=public_runtime(),
        webhook_routes=(webhook_route(),),
    )
    restarted_client = TestClient(TestServer(restarted))
    await restarted_client.start_server()
    await recover_webhook_conversations(restarted)
    try:
        assert restarted_harness.ingested == []
        assert restarted_harness.channel.closed[binding.thread_jid] is True
        restored = restarted_harness.workspace_map.get(binding.thread_jid)
        assert restored is not None
        assert restored.folder == routed_conversation_folder(
            "project",
            message.metadata["conversation_id"],
        )
        assert await get_workspace_profile(binding.thread_jid) == restored
    finally:
        await restarted_client.close()
