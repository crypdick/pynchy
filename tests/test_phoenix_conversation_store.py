from __future__ import annotations

from dataclasses import dataclass

import pytest

import pynchy.conversation.phoenix as phoenix
from pynchy.conversation.events import ConversationEvent, ConversationEventKind
from pynchy.conversation.phoenix import (
    PhoenixConversationStore,
    PhoenixWriteError,
    phoenix_tracer,
)


@dataclass
class StartedSpan:
    name: str
    attributes: dict[str, object] | None = None
    events: list[tuple[str, dict[str, object]]] | None = None

    def __enter__(self) -> StartedSpan:
        self.events = []
        return self

    def __exit__(self, exc_type: object, exc: object, _tb: object) -> None:
        return None

    def add_event(self, name: str, attributes: dict[str, object]) -> None:
        assert self.events is not None
        self.events.append((name, attributes))


class FakeTracer:
    def __init__(self) -> None:
        self.spans: list[StartedSpan] = []

    def start_as_current_span(self, name: str, *, attributes: dict[str, object]) -> StartedSpan:
        span = StartedSpan(name=name, attributes=attributes)
        self.spans.append(span)
        return span


class FailingTracer(FakeTracer):
    def start_as_current_span(self, name: str, *, attributes: dict[str, object]) -> StartedSpan:
        raise RuntimeError("phoenix offline")


class FakeProvider:
    def __init__(self) -> None:
        self.tracer_name: str | None = None

    def get_tracer(self, name: str) -> FakeTracer:
        self.tracer_name = name
        return FakeTracer()


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


async def test_write_event_returns_phoenix_ref() -> None:
    tracer = FakeTracer()
    store = PhoenixConversationStore(tracer=tracer)
    ref = await store.write_event(_event())
    assert ref.trace_ref == "phoenix:event:evt_1"
    assert ref.event_id == "evt_1"
    assert tracer.spans[0].name == "pynchy.conversation.user_message"
    assert tracer.spans[0].attributes["pynchy.content"] == "hello"
    assert tracer.spans[0].events == [("pynchy.conversation.body", {"pynchy.content": "hello"})]


async def test_write_event_wraps_tracer_failures() -> None:
    store = PhoenixConversationStore(tracer=FailingTracer())
    with pytest.raises(PhoenixWriteError, match="Failed to write conversation event evt_1"):
        await store.write_event(_event())


def test_phoenix_tracer_accepts_positional_project_name(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider()
    register_calls: list[dict[str, object]] = []

    def fake_register(**kwargs: object) -> FakeProvider:
        register_calls.append(kwargs)
        return provider

    monkeypatch.setattr(phoenix, "register", fake_register)

    tracer = phoenix_tracer("pynchy")

    assert isinstance(tracer, FakeTracer)
    assert provider.tracer_name == "pynchy.conversation"
    assert register_calls == [
        {
            "project_name": "pynchy",
            "auto_instrument": False,
            "batch": False,
            "protocol": "http/protobuf",
            "verbose": False,
            "set_global_tracer_provider": False,
        }
    ]


def test_phoenix_tracer_forwards_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider()
    register_calls: list[dict[str, object]] = []

    def fake_register(**kwargs: object) -> FakeProvider:
        register_calls.append(kwargs)
        return provider

    monkeypatch.setattr(phoenix, "register", fake_register)

    tracer = phoenix_tracer("pynchy", endpoint="https://example.test/v1/traces")

    assert isinstance(tracer, FakeTracer)
    assert provider.tracer_name == "pynchy.conversation"
    assert register_calls == [
        {
            "project_name": "pynchy",
            "endpoint": "https://example.test/v1/traces",
            "auto_instrument": False,
            "batch": False,
            "protocol": "http/protobuf",
            "verbose": False,
            "set_global_tracer_provider": False,
        }
    ]
