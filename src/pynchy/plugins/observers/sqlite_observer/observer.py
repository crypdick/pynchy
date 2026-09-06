"""SQLite event observer — persists EventBus events to the ``events`` table.

Schema lives in ``db/_schema.py``; storage is delegated to ``db.store_event()``.
"""

from __future__ import annotations

from collections.abc import (
    Awaitable,
    Callable,
)
from dataclasses import dataclass

from pynchy.event_bus import (  # beartype resolves these runtime annotations.
    AgentActivityEvent,
    AgentTraceEvent,
    ChatClearedEvent,
    EventBus,
    MessageEvent,
)
from pynchy.logger import logger
from pynchy.redaction import (
    irreversibly_redact,
)
from pynchy.secrets_scanner import scan_payload_for_secrets

_TRACE_CHAR_LIMIT = 6_000
_TRACE_COLLECTION_LIMIT = 20
_TRACE_DEPTH_LIMIT = 4
_TOOL_NAME_LIMIT = 100
_REDACTED_TRACE_PLACEHOLDER = "[redacted secret-bearing trace content]"


@dataclass
class _TraceBudget:
    remaining: int = _TRACE_CHAR_LIMIT

    def text(self, raw: str) -> str:
        normalized = "".join(
            char if char in {"\n", "\t"} or ord(char) >= 32 else " " for char in raw
        )
        redacted = irreversibly_redact(normalized)
        if redacted == normalized and scan_payload_for_secrets(normalized).secrets_found:
            redacted = _REDACTED_TRACE_PLACEHOLDER
        projected = redacted[: self.remaining]
        self.remaining -= len(projected)
        return projected


def _bounded_trace_value(value: object, budget: _TraceBudget, depth: int = 0) -> object:
    if budget.remaining <= 0:
        projected: object = ""
    elif isinstance(value, str):
        projected = budget.text(value)
    elif value is None or isinstance(value, (bool, int, float)):
        projected = value
    elif depth >= _TRACE_DEPTH_LIMIT:
        projected = budget.text(str(value))
    elif isinstance(value, list):
        projected = [
            _bounded_trace_value(item, budget, depth + 1)
            for item in value[:_TRACE_COLLECTION_LIMIT]
            if budget.remaining > 0
        ]
    elif isinstance(value, dict):
        projected_mapping: dict[str, object] = {}
        for raw_key, item in list(value.items())[:_TRACE_COLLECTION_LIMIT]:
            if budget.remaining <= 0:
                break
            key = budget.text(str(raw_key))
            projected_mapping[key] = _bounded_trace_value(item, budget, depth + 1)
        projected = projected_mapping
    else:
        projected = budget.text(str(value))
    return projected


def _trace_payload(event: AgentTraceEvent) -> dict[str, object]:
    """Build the bounded, irreversibly redacted SQLite evidence projection."""
    payload: dict[str, object] = {"trace_type": event.trace_type}
    budget = _TraceBudget()
    if event.trace_type == "tool_use":
        tool_name = event.data.get("tool_name")
        if isinstance(tool_name, str) and tool_name:
            payload["tool_name"] = budget.text(tool_name)[:_TOOL_NAME_LIMIT]
        payload["tool_input"] = _bounded_trace_value(event.data.get("tool_input", {}), budget)
    elif event.trace_type == "tool_result":
        payload.update(
            {
                "tool_use_id": _bounded_trace_value(event.data.get("tool_use_id", ""), budget),
                "content": _bounded_trace_value(event.data.get("content", ""), budget),
                "is_error": bool(event.data.get("is_error", False)),
            }
        )
    elif event.trace_type == "text":
        payload["text"] = _bounded_trace_value(event.data.get("text", ""), budget)
    return payload


class SqliteEventObserver:
    """Persists all EventBus events to a ``events`` table in the main database."""

    name = "sqlite"

    def __init__(
        self,
        *,
        store_event: Callable[[str, str | None, dict[str, object]], Awaitable[None]],
    ) -> None:
        self._unsubs: list[Callable[[], None]] = []
        self._store_event = store_event

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
        """Persist a safe projection for Cop context and live evidence review."""
        # NOTE: Update docs/architecture/observers.md § Built-in: sqlite-observer
        # when changing this durable trace projection.
        await self._store("agent_trace", event.chat_jid, _trace_payload(event))

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
            await self._store_event(event_type, chat_jid, payload)
        except Exception as exc:  # noqa: BLE001 - event persistence is best-effort observer behavior.
            logger.warning(
                "SQLite observer failed to store event",
                err=str(exc),
                event_type=event_type,
            )
