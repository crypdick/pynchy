"""Lightweight asyncio event bus for intra-process pub/sub."""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from pynchy.logger import logger

# --- Event types ---


@dataclass
class MessageEvent:
    """A message was stored (inbound or outbound)."""

    chat_jid: str
    sender_name: str
    content: str
    timestamp: str
    is_bot: bool


@dataclass
class AgentActivityEvent:
    """Agent started/stopped processing for a group."""

    chat_jid: str
    active: bool


@dataclass
class AgentTraceEvent:
    """Ephemeral agent trace — thinking, tool use, intermediate text."""

    chat_jid: str
    trace_type: str  # "thinking", "tool_use", "text"
    data: dict[str, Any]


@dataclass
class ChatClearedEvent:
    """Chat history was cleared (messages archived, not deleted)."""

    chat_jid: str


type Event = MessageEvent | AgentActivityEvent | AgentTraceEvent | ChatClearedEvent
type Listener = Callable[[Any], Coroutine[Any, Any, None]]


class EventBus:
    """Fire-and-forget async event dispatcher."""

    def __init__(self) -> None:
        self._listeners: defaultdict[type, list[Listener]] = defaultdict(list)
        # Fire-and-forget tasks need a strong reference somewhere or the event
        # loop may garbage-collect them mid-execution; this set holds them
        # until each completes (self-removing via the done callback).
        self._background_tasks: set[asyncio.Task[None]] = set()

    def subscribe(self, event_type: type, listener: Listener) -> Callable[[], None]:
        """Subscribe to an event type. Returns an unsubscribe function."""
        self._listeners[event_type].append(listener)

        def _unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._listeners[event_type].remove(listener)

        return _unsubscribe

    def emit(self, event: Event) -> None:
        """Emit an event to all subscribers. Non-blocking, fire-and-forget."""
        for listener in self._listeners[type(event)]:
            task = asyncio.create_task(_safe_call(listener, event))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)


async def _safe_call(listener: Listener, event: Event) -> None:
    try:
        await listener(event)
    except Exception:
        logger.exception("EventBus listener error")
