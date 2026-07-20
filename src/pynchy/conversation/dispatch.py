"""Process-local wake callbacks for durable conversation deliveries."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pynchy.conversation.models import ConversationDeliveryCompletion
from pynchy.logger import logger

ConversationDeliveryWaker = Callable[[ConversationDeliveryCompletion], Awaitable[None]]

_wakers: dict[str, dict[object, ConversationDeliveryWaker]] = {}


def register_conversation_delivery_waker(
    provider: str,
    owner: object,
    waker: ConversationDeliveryWaker,
) -> None:
    """Register one runtime owner that can dispatch a provider's next delivery."""
    _wakers.setdefault(provider, {})[owner] = waker


def unregister_conversation_delivery_waker(provider: str, owner: object) -> None:
    """Remove one runtime owner's wake callback."""
    providers = _wakers.get(provider)
    if providers is None:
        return
    providers.pop(owner, None)
    if not providers:
        _wakers.pop(provider, None)


async def notify_conversation_delivery_completed(
    delivery: ConversationDeliveryCompletion,
) -> None:
    """Wake adapters that may own the completed delivery's pending sibling."""
    for waker in tuple(_wakers.get(delivery.identity.provider, {}).values()):
        try:
            await waker(delivery)
        except Exception:  # noqa: BLE001, RUF100 - committed turns must not roll back when a sibling wake fails.
            logger.exception(
                "Conversation delivery sibling wake failed",
                provider=delivery.identity.provider,
                route=delivery.identity.route,
                conversation_id=delivery.conversation_id,
            )
