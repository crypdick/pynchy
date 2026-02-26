"""SQLite event observer — persists EventBus events to the ``events`` table.

Schema lives in ``db/_schema.py``; storage is delegated to ``db.store_event()``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pynchy.logger import logger

if TYPE_CHECKING:
    from pynchy.event_bus import EventBus


class SqliteEventObserver:
    """Persists all EventBus events to a ``events`` table in the main database."""

    name = "sqlite"

    def __init__(self) -> None:
        self._unsubs: list[Callable[[], None]] = []

    def subscribe(self, event_bus: EventBus) -> None:
        """Subscribe to all event types and persist each to SQLite."""
        from pynchy.event_bus import (
            AgentActivityEvent,
            AgentTraceEvent,
            ChatClearedEvent,
            MessageEvent,
        )

        self._unsubs.append(event_bus.subscribe(MessageEvent, self._on_message))
        self._unsubs.append(event_bus.subscribe(AgentActivityEvent, self._on_activity))
        self._unsubs.append(event_bus.subscribe(AgentTraceEvent, self._on_trace))
        self._unsubs.append(event_bus.subscribe(ChatClearedEvent, self._on_clear))

    async def close(self) -> None:
        """Unsubscribe from all events."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _on_message(self, event: Any) -> None:
        await self._store(
            "message",
            event.chat_jid,
            {
                "sender_name": event.sender_name,
                "content": event.content[:500],  # truncate for storage
                "is_bot": event.is_bot,
            },
        )

    async def _on_activity(self, event: Any) -> None:
        await self._store(
            "agent_activity",
            event.chat_jid,
            {"active": event.active},
        )

    async def _on_trace(self, event: Any) -> None:
        await self._store(
            "agent_trace",
            event.chat_jid,
            {"trace_type": event.trace_type, **event.data},
        )

    async def _on_clear(self, event: Any) -> None:
        await self._store("chat_cleared", event.chat_jid, {})

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    async def _store(self, event_type: str, chat_jid: str | None, payload: dict) -> None:
        try:
            from pynchy.db import store_event

            await store_event(event_type, chat_jid, payload)
        except Exception as exc:
            logger.warning(
                "SQLite observer failed to store event",
                err=str(exc),
                event_type=event_type,
            )
