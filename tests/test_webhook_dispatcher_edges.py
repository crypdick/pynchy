"""Boundary coverage for routed webhook dispatcher ownership and recovery."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest
from linear_webhook_test_support import LinearWebhookHarness

from pynchy.conversation.api import ConversationDeliveryCompletion, ConversationId
from pynchy.host.orchestrator.webhook_conversations import WebhookConversationDispatcher
from tests.webhook_lifecycle_support import (
    _admit,
    _delivery_identity,
    _message_event,
    _route,
)

pytest_plugins = ("tests.webhook_lifecycle_support",)


@pytest.mark.parametrize(
    "event",
    [
        _message_event("missing-control-state", closed=None),
        _message_event("closed-control-state", closed=True),
        replace(_message_event("missing-conversation"), conversation=None),
    ],
    ids=["missing-control-state", "closed-control-state", "missing-conversation"],
)
async def test_project_open_control_ignores_non_open_snapshots(event) -> None:
    dispatcher = WebhookConversationDispatcher(deps=MagicMock(), routes=(_route(),))

    assert await dispatcher.project_open_control(_route(), event) is None


async def test_wake_rejects_delivery_for_an_unavailable_route() -> None:
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    original_route = _route()
    original_dispatcher = WebhookConversationDispatcher(deps=harness, routes=(original_route,))
    conversation_id = await _admit(
        original_dispatcher,
        original_route,
        _message_event("unavailable-route"),
    )
    unavailable_route = replace(original_route, name="different-route")
    restarted_dispatcher = WebhookConversationDispatcher(
        deps=harness,
        routes=(unavailable_route,),
    )

    with pytest.raises(RuntimeError, match="unavailable route"):
        await restarted_dispatcher.wake(conversation_id)


async def test_after_completion_wakes_when_control_sync_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = LinearWebhookHarness()
    route = _route()
    dispatcher = WebhookConversationDispatcher(deps=harness, routes=(route,))
    wake = AsyncMock()
    monkeypatch.setattr(WebhookConversationDispatcher, "wake", wake)
    sync_state = AsyncMock(side_effect=RuntimeError("database temporarily unavailable"))
    monkeypatch.setattr(
        "pynchy.host.orchestrator.webhook_conversations.sync_conversation_control_state",
        sync_state,
    )
    completion = ConversationDeliveryCompletion(
        identity=_delivery_identity(route, "completed-delivery"),
        conversation_id=ConversationId("conversation-1"),
    )

    await dispatcher.after_completion(completion)

    sync_state.assert_awaited_once_with(harness.channels(), completion.conversation_id)
    wake.assert_awaited_once_with(completion.conversation_id)
