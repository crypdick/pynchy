"""Conversation delivery wake-up boundary contracts."""

from __future__ import annotations

from asyncio import sleep

from pynchy.conversation.api import (
    ConversationDeliveryCompletion,
    ConversationId,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
    notify_conversation_delivery_completed,
    register_conversation_delivery_waker,
    unregister_conversation_delivery_waker,
)


def _completion(provider: str = "provider") -> ConversationDeliveryCompletion:
    return ConversationDeliveryCompletion(
        identity=ExternalDeliveryIdentity(
            provider=ExternalProvider(provider),
            route=ExternalRoute("route"),
            delivery_id=ExternalDeliveryId("delivery"),
        ),
        conversation_id=ConversationId("conversation"),
    )


def test_unregister_unknown_delivery_waker_is_a_noop() -> None:
    unregister_conversation_delivery_waker("unregistered-provider", object())


async def test_delivery_waker_failure_does_not_block_other_wakers() -> None:
    owner = object()
    received: list[ConversationDeliveryCompletion] = []

    async def failing_waker(_delivery: ConversationDeliveryCompletion) -> None:
        await sleep(0)
        raise RuntimeError("wake failed")

    async def recording_waker(delivery: ConversationDeliveryCompletion) -> None:
        await sleep(0)
        received.append(delivery)

    register_conversation_delivery_waker("provider", owner, failing_waker)
    register_conversation_delivery_waker("provider", "recording", recording_waker)
    try:
        await notify_conversation_delivery_completed(_completion())
    finally:
        unregister_conversation_delivery_waker("provider", owner)
        unregister_conversation_delivery_waker("provider", "recording")

    assert received == [_completion()]
