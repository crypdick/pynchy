"""Tests for the EventBus pub/sub system."""

from __future__ import annotations

import asyncio

import pytest

from pynchy.event_bus import (
    AgentActivityEvent,
    AgentTraceEvent,
    ChatClearedEvent,
    EventBus,
    MessageEvent,
)


@pytest.fixture
def bus() -> EventBus:
    """Create a fresh EventBus for each test."""
    return EventBus()


class TestEventBus:
    """Test EventBus subscription and emission."""

    @pytest.mark.asyncio
    async def test_subscribe_and_emit_message_event(self, bus: EventBus) -> None:
        """Test subscribing to and emitting a MessageEvent."""
        received: list[MessageEvent] = []

        async def listener(event: MessageEvent) -> None:
            received.append(event)

        bus.subscribe(MessageEvent, listener)

        event = MessageEvent(
            chat_jid="test@jid",
            sender_name="Alice",
            content="Hello world",
            timestamp="2026-02-14T00:00:00Z",
            is_bot=False,
        )
        bus.emit(event)

        # Give event loop time to process
        await asyncio.sleep(0.01)

        assert len(received) == 1
        assert received[0] == event

    @pytest.mark.asyncio
    async def test_subscribe_and_emit_agent_activity_event(self, bus: EventBus) -> None:
        """Test subscribing to and emitting an AgentActivityEvent."""
        received: list[AgentActivityEvent] = []

        async def listener(event: AgentActivityEvent) -> None:
            received.append(event)

        bus.subscribe(AgentActivityEvent, listener)

        event = AgentActivityEvent(chat_jid="test@jid", active=True)
        bus.emit(event)

        await asyncio.sleep(0.01)

        assert len(received) == 1
        assert received[0] == event

    @pytest.mark.asyncio
    async def test_subscribe_and_emit_agent_trace_event(self, bus: EventBus) -> None:
        """Test subscribing to and emitting an AgentTraceEvent."""
        received: list[AgentTraceEvent] = []

        async def listener(event: AgentTraceEvent) -> None:
            received.append(event)

        bus.subscribe(AgentTraceEvent, listener)

        event = AgentTraceEvent(
            chat_jid="test@jid",
            trace_type="thinking",
            data={"content": "Processing request..."},
        )
        bus.emit(event)

        await asyncio.sleep(0.01)

        assert len(received) == 1
        assert received[0] == event

    @pytest.mark.asyncio
    async def test_subscribe_and_emit_chat_cleared_event(self, bus: EventBus) -> None:
        """Test subscribing to and emitting a ChatClearedEvent."""
        received: list[ChatClearedEvent] = []

        async def listener(event: ChatClearedEvent) -> None:
            received.append(event)

        bus.subscribe(ChatClearedEvent, listener)

        event = ChatClearedEvent(chat_jid="test@jid")
        bus.emit(event)

        await asyncio.sleep(0.01)

        assert len(received) == 1
        assert received[0] == event

    @pytest.mark.asyncio
    async def test_multiple_listeners_same_event_type(self, bus: EventBus) -> None:
        """Test that multiple listeners can subscribe to the same event type."""
        received_1: list[MessageEvent] = []
        received_2: list[MessageEvent] = []

        async def listener1(event: MessageEvent) -> None:
            received_1.append(event)

        async def listener2(event: MessageEvent) -> None:
            received_2.append(event)

        bus.subscribe(MessageEvent, listener1)
        bus.subscribe(MessageEvent, listener2)

        event = MessageEvent(
            chat_jid="test@jid",
            sender_name="Bob",
            content="Test",
            timestamp="2026-02-14T00:00:00Z",
            is_bot=True,
        )
        bus.emit(event)

        await asyncio.sleep(0.01)

        assert len(received_1) == 1
        assert len(received_2) == 1
        assert received_1[0] == event
        assert received_2[0] == event

    @pytest.mark.asyncio
    async def test_event_type_isolation(self, bus: EventBus) -> None:
        """Test that different event types don't interfere with each other."""
        message_received: list[MessageEvent] = []
        activity_received: list[AgentActivityEvent] = []

        async def message_listener(event: MessageEvent) -> None:
            message_received.append(event)

        async def activity_listener(event: AgentActivityEvent) -> None:
            activity_received.append(event)

        bus.subscribe(MessageEvent, message_listener)
        bus.subscribe(AgentActivityEvent, activity_listener)

        message_event = MessageEvent(
            chat_jid="test@jid",
            sender_name="Alice",
            content="Hi",
            timestamp="2026-02-14T00:00:00Z",
            is_bot=False,
        )
        activity_event = AgentActivityEvent(chat_jid="test@jid", active=True)

        bus.emit(message_event)
        bus.emit(activity_event)

        await asyncio.sleep(0.01)

        assert len(message_received) == 1
        assert len(activity_received) == 1
        assert message_received[0] == message_event
        assert activity_received[0] == activity_event

    @pytest.mark.asyncio
    async def test_unsubscribe(self, bus: EventBus) -> None:
        """Test that unsubscribe function works correctly."""
        received: list[MessageEvent] = []

        async def listener(event: MessageEvent) -> None:
            received.append(event)

        unsubscribe = bus.subscribe(MessageEvent, listener)

        event1 = MessageEvent(
            chat_jid="test@jid",
            sender_name="Alice",
            content="First",
            timestamp="2026-02-14T00:00:00Z",
            is_bot=False,
        )
        bus.emit(event1)
        await asyncio.sleep(0.01)

        assert len(received) == 1

        # Unsubscribe
        unsubscribe()

        event2 = MessageEvent(
            chat_jid="test@jid",
            sender_name="Alice",
            content="Second",
            timestamp="2026-02-14T00:00:01Z",
            is_bot=False,
        )
        bus.emit(event2)
        await asyncio.sleep(0.01)

        # Should still be 1, not 2
        assert len(received) == 1
        assert received[0] == event1

    @pytest.mark.asyncio
    async def test_emit_without_subscribers(self, bus: EventBus) -> None:
        """Test that emitting without subscribers doesn't cause errors."""
        event = MessageEvent(
            chat_jid="test@jid",
            sender_name="Alice",
            content="Nobody listening",
            timestamp="2026-02-14T00:00:00Z",
            is_bot=False,
        )
        # Should not raise
        bus.emit(event)
        await asyncio.sleep(0.01)

    @pytest.mark.asyncio
    async def test_listener_exception_does_not_propagate(self, bus: EventBus) -> None:
        """Test that exceptions in listeners don't propagate or affect other listeners."""
        received_good: list[MessageEvent] = []

        async def bad_listener(event: MessageEvent) -> None:
            raise ValueError("Intentional error")

        async def good_listener(event: MessageEvent) -> None:
            received_good.append(event)

        bus.subscribe(MessageEvent, bad_listener)
        bus.subscribe(MessageEvent, good_listener)

        event = MessageEvent(
            chat_jid="test@jid",
            sender_name="Alice",
            content="Test exception handling",
            timestamp="2026-02-14T00:00:00Z",
            is_bot=False,
        )
        bus.emit(event)

        await asyncio.sleep(0.01)

        # Good listener should still receive the event
        assert len(received_good) == 1
        assert received_good[0] == event

    @pytest.mark.asyncio
    async def test_multiple_unsubscribe_calls(self, bus: EventBus) -> None:
        """Test that calling unsubscribe multiple times doesn't cause errors."""
        received: list[MessageEvent] = []

        async def listener(event: MessageEvent) -> None:
            received.append(event)

        unsubscribe = bus.subscribe(MessageEvent, listener)

        # Call unsubscribe twice — second call is a no-op (safe to call twice)
        unsubscribe()
        unsubscribe()  # Should not raise

    @pytest.mark.asyncio
    async def test_fire_and_forget_behavior(self, bus: EventBus) -> None:
        """Test that emit() returns immediately (fire-and-forget)."""
        processing_started = asyncio.Event()
        processing_done = asyncio.Event()

        async def slow_listener(event: MessageEvent) -> None:
            processing_started.set()
            await asyncio.sleep(0.1)  # Simulate slow processing
            processing_done.set()

        bus.subscribe(MessageEvent, slow_listener)

        event = MessageEvent(
            chat_jid="test@jid",
            sender_name="Alice",
            content="Test",
            timestamp="2026-02-14T00:00:00Z",
            is_bot=False,
        )

        # emit() should return immediately
        bus.emit(event)

        # Processing should not be done yet
        assert not processing_done.is_set()

        # Wait for processing to start and complete
        await asyncio.wait_for(processing_started.wait(), timeout=1.0)
        await asyncio.wait_for(processing_done.wait(), timeout=1.0)

    @pytest.mark.asyncio
    async def test_concurrent_emissions(self, bus: EventBus) -> None:
        """Test that multiple concurrent emissions work correctly."""
        received: list[MessageEvent] = []

        async def listener(event: MessageEvent) -> None:
            await asyncio.sleep(0.01)  # Small delay
            received.append(event)

        bus.subscribe(MessageEvent, listener)

        # Emit multiple events rapidly
        events = [
            MessageEvent(
                chat_jid=f"test@jid{i}",
                sender_name=f"User{i}",
                content=f"Message {i}",
                timestamp="2026-02-14T00:00:00Z",
                is_bot=False,
            )
            for i in range(10)
        ]

        for event in events:
            bus.emit(event)

        # Wait for all to process
        await asyncio.sleep(0.2)

        assert len(received) == 10
        # All events should have been received (order not guaranteed)
        assert set(e.content for e in received) == {f"Message {i}" for i in range(10)}
