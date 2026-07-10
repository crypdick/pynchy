from __future__ import annotations

import asyncio

import pytest

from pynchy.conversation.events import ConversationEvent, ConversationEventKind
from pynchy.conversation.phoenix import PhoenixEventRef, PhoenixWriteError
from pynchy.conversation.sink import ConversationSink


class FakeStore:
    def __init__(self, *, fail: bool = False, calls: list[str] | None = None) -> None:
        self.fail = fail
        self.calls = calls
        self.writes: list[ConversationEvent] = []

    async def write_event(self, event: ConversationEvent) -> PhoenixEventRef:
        if self.fail:
            raise PhoenixWriteError("no phoenix")
        if self.calls is not None:
            self.calls.append(f"phoenix:{event.event_id}")
        self.writes.append(event)
        return PhoenixEventRef(
            event.event_id,
            f"phoenix:trace:trace_{event.event_id}:span:span_{event.event_id}:event:{event.event_id}",
        )


def _event() -> ConversationEvent:
    return ConversationEvent(
        event_id="evt_1",
        turn_id="turn_1",
        chat_jid="slack:C123",
        timestamp="2026-07-10T00:00:00+00:00",
        kind=ConversationEventKind.USER_MESSAGE,
        sender="alice",
        sender_name="Alice",
        content="hello",
        message_type="user",
    )


async def test_sink_writes_phoenix_before_projection() -> None:
    calls: list[str] = []

    async def store_pointer(event: ConversationEvent, ref: PhoenixEventRef) -> None:
        await asyncio.sleep(0)
        calls.append(f"sqlite:{event.event_id}:{ref.trace_ref}")

    store = FakeStore(calls=calls)
    sink = ConversationSink(body_store=store, store_pointer=store_pointer)
    ref = await sink.append(_event())
    assert ref.trace_ref == "phoenix:trace:trace_evt_1:span:span_evt_1:event:evt_1"
    assert [event.event_id for event in store.writes] == ["evt_1"]
    assert calls == [
        "phoenix:evt_1",
        "sqlite:evt_1:phoenix:trace:trace_evt_1:span:span_evt_1:event:evt_1",
    ]


async def test_sink_does_not_project_when_phoenix_fails() -> None:
    calls: list[str] = []

    async def store_pointer(event: ConversationEvent, ref: PhoenixEventRef) -> None:
        await asyncio.sleep(0)
        calls.append(event.event_id)

    sink = ConversationSink(body_store=FakeStore(fail=True), store_pointer=store_pointer)
    with pytest.raises(PhoenixWriteError):
        await sink.append(_event())
    assert calls == []
