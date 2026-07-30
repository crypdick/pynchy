"""Public SQLite observer event contracts."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, call

import pytest

from pynchy.event_bus import (
    AgentActivityEvent,
    ChatClearedEvent,
    Event,
    EventBus,
    MessageEvent,
)
from pynchy.plugins.observers.sqlite_observer.observer import SqliteEventObserver


async def _emit(bus: EventBus, event: Event) -> None:
    bus.emit(event)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_observer_persists_message_activity_and_clear_events() -> None:
    store = AsyncMock()
    observer = SqliteEventObserver(store_event=store)
    bus = EventBus()
    observer.subscribe(bus)

    await _emit(
        bus,
        MessageEvent(
            chat_jid="chat",
            sender_name="Test Sender",
            content="x" * 501,
            timestamp="now",
            is_bot=False,
        ),
    )
    await _emit(bus, AgentActivityEvent(chat_jid="chat", active=True))
    await _emit(bus, ChatClearedEvent(chat_jid="chat"))

    assert store.await_args_list == [
        call(
            "message",
            "chat",
            {"sender_name": "Test Sender", "content": "x" * 500, "is_bot": False},
        ),
        call("agent_activity", "chat", {"active": True}),
        call("chat_cleared", "chat", {}),
    ]


@pytest.mark.asyncio
async def test_observer_close_unsubscribes_from_future_events() -> None:
    store = AsyncMock()
    observer = SqliteEventObserver(store_event=store)
    bus = EventBus()
    observer.subscribe(bus)

    await observer.close()
    await _emit(bus, AgentActivityEvent(chat_jid="chat", active=False))

    store.assert_not_awaited()


@pytest.mark.asyncio
async def test_observer_contains_store_failures_at_event_boundary() -> None:
    store = AsyncMock(side_effect=RuntimeError("database unavailable"))
    observer = SqliteEventObserver(store_event=store)
    bus = EventBus()
    observer.subscribe(bus)

    await _emit(bus, ChatClearedEvent(chat_jid="chat"))

    store.assert_awaited_once_with("chat_cleared", "chat", {})
