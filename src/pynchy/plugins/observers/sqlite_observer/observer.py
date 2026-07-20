"""SQLite event observer — persists EventBus events to the ``events`` table.

Schema lives in ``db/_schema.py``; storage is delegated to ``db.store_event()``.
"""

from __future__ import annotations

from collections.abc import (
    Callable,  # noqa: TC003, RUF100 - beartype resolves this runtime annotation.
)

from pynchy import state
from pynchy.event_bus import (  # noqa: TC001, RUF100 - beartype resolves these runtime annotations.
    AgentActivityEvent,
    AgentTraceEvent,
    ChatClearedEvent,
    EventBus,
    MessageEvent,
)
from pynchy.logger import logger


class SqliteEventObserver:
    """Persists all EventBus events to a ``events`` table in the main database."""

    name = "sqlite"

    def __init__(self) -> None:
        self._unsubs: list[Callable[[], None]] = []

    def subscribe(self, event_bus: EventBus) -> None:
        """Subscribe to all event types and persist each to SQLite."""
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

    async def _on_message(self, event: MessageEvent) -> None:
        await self._store(
            "message",
            event.chat_jid,
            {
                "sender_name": event.sender_name,
                "content": event.content[:500],  # truncate for storage
                "is_bot": event.is_bot,
            },
        )

    async def _on_activity(self, event: AgentActivityEvent) -> None:
        await self._store(
            "agent_activity",
            event.chat_jid,
            {"active": event.active},
        )

    async def _on_trace(self, event: AgentTraceEvent) -> None:
        """Persist only the semantic action name needed by bounded Cop context."""
        payload: dict[str, object] = {"trace_type": event.trace_type}
        if event.trace_type == "tool_use":
            tool_name = event.data.get("tool_name")
            if isinstance(tool_name, str) and tool_name:
                payload["tool_name"] = tool_name[:100]
        await self._store("agent_trace", event.chat_jid, payload)

    async def _on_clear(self, event: ChatClearedEvent) -> None:
        await self._store("chat_cleared", event.chat_jid, {})

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    async def _store(
        self,
        event_type: str,
        chat_jid: str | None,
        payload: dict[str, object],
    ) -> None:
        try:
            await state.store_event(event_type, chat_jid, payload)
        except Exception as exc:  # noqa: BLE001, RUF100 - event persistence is best-effort observer behavior.
            logger.warning(
                "SQLite observer failed to store event",
                err=str(exc),
                event_type=event_type,
            )
