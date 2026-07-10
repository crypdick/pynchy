from __future__ import annotations

import pytest

from pynchy.conversation.events import ConversationEvent, ConversationEventKind
from pynchy.conversation.phoenix import PhoenixEventRef
from pynchy.state import (
    get_conversation_event_pointers_since,
    get_messages_since,
    init_test_database,
    store_chat_metadata,
    store_conversation_event_pointer,
    store_message_direct,
)
from pynchy.state.conversation_events import (
    _decode_metadata,  # noqa: PLC2701  # allow: private-test-imports - review requested decoder coverage.
)


def _event(
    event_id: str,
    timestamp: str,
    metadata: dict[str, object] | None = None,
    content: str | None = None,
    source_message_id: str | None = None,
) -> ConversationEvent:
    return ConversationEvent(
        event_id=event_id,
        turn_id="turn_1",
        chat_jid="slack:C123",
        timestamp=timestamp,
        kind=ConversationEventKind.USER_MESSAGE,
        sender="alice",
        sender_name="Alice",
        content=content if content is not None else f"body {event_id}",
        message_type="user",
        source_message_id=source_message_id,
        metadata=metadata or {"source": "test"},
    )


class FakeBodyReader:
    def __init__(self, bodies: dict[str, str]) -> None:
        self._bodies = bodies
        self.calls: list[str] = []

    async def read_event_content(self, event_id: str) -> str:
        self.calls.append(event_id)
        if event_id not in self._bodies:
            raise RuntimeError(f"missing content for {event_id}")
        return self._bodies[event_id]


async def test_store_and_load_projection_pointer() -> None:
    await init_test_database()
    event = _event("evt_1", "2026-07-10T00:00:00+00:00")
    await store_conversation_event_pointer(
        event,
        PhoenixEventRef("evt_1", "phoenix:event:evt_1"),
    )

    rows = await get_conversation_event_pointers_since("slack:C123", None)

    assert len(rows) == 1
    assert rows[0]["event_id"] == "evt_1"
    assert rows[0]["content_preview"] == "body evt_1"
    assert rows[0]["phoenix_ref"] == "phoenix:event:evt_1"
    assert rows[0]["metadata"] == {"source": "test"}


async def test_since_filter_is_exclusive() -> None:
    await init_test_database()
    await store_conversation_event_pointer(
        _event("evt_1", "2026-07-10T00:00:00+00:00"),
        PhoenixEventRef("evt_1", "phoenix:event:evt_1"),
    )
    await store_conversation_event_pointer(
        _event("evt_2", "2026-07-10T00:01:00+00:00"),
        PhoenixEventRef("evt_2", "phoenix:event:evt_2"),
    )

    rows = await get_conversation_event_pointers_since(
        "slack:C123",
        "2026-07-10T00:00:00+00:00",
    )

    assert [row["event_id"] for row in rows] == ["evt_2"]


async def test_store_projection_pointer_decodes_nested_metadata() -> None:
    await init_test_database()
    event = _event(
        "evt_1",
        "2026-07-10T00:00:00+00:00",
        metadata={"source": "test", "nested": {"items": ["one", {"two": 2}]}},
    )

    await store_conversation_event_pointer(
        event,
        PhoenixEventRef("evt_1", "phoenix:event:evt_1"),
    )

    rows = await get_conversation_event_pointers_since("slack:C123", None)

    assert rows[0]["metadata"] == {
        "source": "test",
        "nested": {"items": ["one", {"two": 2}]},
    }


async def test_get_messages_since_includes_projected_conversation_events() -> None:
    await init_test_database()
    await store_conversation_event_pointer(
        _event("evt_1", "2026-07-10T00:00:00+00:00"),
        PhoenixEventRef("evt_1", "phoenix:event:evt_1"),
    )
    messages = await get_messages_since(
        "slack:C123",
        "",
        body_reader=FakeBodyReader({"evt_1": "body evt_1"}),
    )
    assert len(messages) == 1
    assert messages[0].id == "evt_1"
    assert messages[0].content == "body evt_1"
    assert messages[0].metadata is not None
    assert messages[0].metadata["phoenix_ref"] == "phoenix:event:evt_1"
    assert messages[0].metadata["source_message_id"] is None


async def test_get_messages_since_hydrates_projected_full_body_from_phoenix() -> None:
    await init_test_database()
    preview_source = "line one\n" + ("x " * 700)
    phoenix_body = "line one\n" + ("x " * 700) + "\nline after preview"
    await store_conversation_event_pointer(
        _event(
            "evt_long",
            "2026-07-10T00:00:00+00:00",
            content=preview_source,
            source_message_id="source_long",
        ),
        PhoenixEventRef("evt_long", "phoenix:event:evt_long"),
    )

    messages = await get_messages_since(
        "slack:C123",
        "",
        body_reader=FakeBodyReader({"evt_long": phoenix_body}),
    )

    assert len(messages) == 1
    assert len(messages[0].content) > 500
    assert messages[0].content == phoenix_body
    assert messages[0].content != preview_source[:500] + "..."
    assert messages[0].metadata is not None
    assert messages[0].metadata["source_message_id"] == "source_long"


async def test_get_messages_since_raises_when_projected_body_hydration_fails() -> None:
    await init_test_database()
    await store_conversation_event_pointer(
        _event("evt_missing", "2026-07-10T00:00:00+00:00", content="preview only"),
        PhoenixEventRef("evt_missing", "phoenix:event:evt_missing"),
    )

    with pytest.raises(RuntimeError, match="evt_missing"):
        await get_messages_since("slack:C123", "", body_reader=FakeBodyReader({}))


async def test_get_messages_since_orders_legacy_and_hydrated_projected_rows() -> None:
    await init_test_database()
    await store_chat_metadata("slack:C123", "2026-07-10T00:00:00+00:00")
    await store_message_direct(
        message_id="legacy_1",
        chat_jid="slack:C123",
        sender="bob",
        sender_name="Bob",
        content="legacy body",
        timestamp="2026-07-10T00:00:02+00:00",
        is_from_me=False,
    )
    await store_conversation_event_pointer(
        _event("evt_1", "2026-07-10T00:00:01+00:00"),
        PhoenixEventRef("evt_1", "phoenix:event:evt_1"),
    )

    messages = await get_messages_since(
        "slack:C123",
        "",
        body_reader=FakeBodyReader({"evt_1": "projected body"}),
    )

    assert [message.id for message in messages] == ["evt_1", "legacy_1"]
    assert [message.content for message in messages] == ["projected body", "legacy body"]


async def test_get_messages_since_prefers_legacy_row_over_duplicate_projection() -> None:
    await init_test_database()
    await store_chat_metadata("slack:C123", "2026-07-10T00:00:00+00:00")
    await store_message_direct(
        message_id="source_1",
        chat_jid="slack:C123",
        sender="alice",
        sender_name="Alice",
        content="legacy full body",
        timestamp="2026-07-10T00:00:00+00:00",
        is_from_me=False,
    )
    reader = FakeBodyReader({"evt_1": "projected duplicate body"})
    await store_conversation_event_pointer(
        _event(
            "evt_1",
            "2026-07-10T00:00:01+00:00",
            source_message_id="source_1",
        ),
        PhoenixEventRef("evt_1", "phoenix:event:evt_1"),
    )

    messages = await get_messages_since("slack:C123", "", body_reader=reader)

    assert [message.id for message in messages] == ["source_1"]
    assert messages[0].content == "legacy full body"
    assert reader.calls == []


async def test_get_messages_since_excludes_projected_assistant_events() -> None:
    await init_test_database()
    await store_conversation_event_pointer(
        ConversationEvent(
            event_id="evt_1",
            turn_id="turn_1",
            chat_jid="slack:C123",
            timestamp="2026-07-10T00:00:00+00:00",
            kind=ConversationEventKind.ASSISTANT_MESSAGE,
            sender="assistant",
            sender_name="Pynchy",
            content="body evt_1",
            message_type="assistant",
            metadata={"source": "test"},
        ),
        PhoenixEventRef("evt_1", "phoenix:event:evt_1"),
    )

    messages = await get_messages_since("slack:C123", "")

    assert messages == []


async def test_store_projection_pointer_rejects_mismatched_phoenix_ref() -> None:
    await init_test_database()
    event = _event("evt_1", "2026-07-10T00:00:00+00:00")

    with pytest.raises(
        ValueError,
        match="Phoenix ref event_id 'evt_2' does not match event 'evt_1'",
    ):
        await store_conversation_event_pointer(
            event,
            PhoenixEventRef("evt_2", "phoenix:event:evt_2"),
        )

    rows = await get_conversation_event_pointers_since("slack:C123", None)
    assert rows == []


def test_decode_metadata_returns_empty_dict_for_malformed_json() -> None:
    assert _decode_metadata("{not valid json") == {}
