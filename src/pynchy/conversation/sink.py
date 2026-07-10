from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from pynchy.conversation.events import ConversationEvent
from pynchy.conversation.phoenix import PhoenixEventRef
from pynchy.state.conversation_events import store_conversation_event_pointer

if TYPE_CHECKING:
    from pynchy.conversation.phoenix import ConversationBodyStore
else:
    ConversationBodyStore = Any


StorePointer = Callable[[ConversationEvent, PhoenixEventRef], Awaitable[None]]


class ConversationSink:
    def __init__(
        self,
        *,
        body_store: ConversationBodyStore,
        store_pointer: StorePointer = store_conversation_event_pointer,
    ) -> None:
        self._body_store = body_store
        self._store_pointer = store_pointer

    async def append(self, event: ConversationEvent) -> PhoenixEventRef:
        ref = await self._body_store.write_event(event)
        await self._store_pointer(event, ref)
        return ref
