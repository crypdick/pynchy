from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from pynchy.conversation.events import (
    ConversationEvent,
    ConversationEventKind,
    content_preview,
    new_turn_id,
)


def _event(**overrides: Any) -> ConversationEvent:
    values: dict[str, Any] = {
        "event_id": "evt_1",
        "turn_id": "turn_1",
        "chat_jid": "slack:C123",
        "timestamp": "2026-07-10T00:00:00+00:00",
        "kind": ConversationEventKind.USER_MESSAGE,
        "sender": "alice",
        "sender_name": None,
        "content": "hello",
        "message_type": "user",
        "source_message_id": None,
        "metadata": {},
    }
    values.update(overrides)
    return ConversationEvent(**values)


def test_new_ids_are_prefixed_and_distinct() -> None:
    assert new_turn_id().startswith("turn_")
    assert new_turn_id() != new_turn_id()


def test_content_preview_collapses_whitespace_and_marks_truncation() -> None:
    text = "alpha\n\nbeta\tgamma " + ("x" * 600)
    preview = content_preview(text, limit=32)
    assert preview == "alpha beta gamma xxxxxxxxxxxxxxx..."
    assert len(preview) == 35


def test_content_preview_accepts_positional_limit() -> None:
    assert content_preview("hello", 2) == "he..."


def test_conversation_event_metadata_omits_empty_values() -> None:
    event = _event(metadata={"slack_ts": "1.23"})
    attrs = event.span_attributes()
    assert attrs["pynchy.event_id"] == "evt_1"
    assert attrs["pynchy.turn_id"] == "turn_1"
    assert attrs["pynchy.kind"] == "user_message"
    assert attrs["pynchy.chat_jid"] == "slack:C123"
    assert attrs["pynchy.sender"] == "alice"
    assert "pynchy.sender_name" not in attrs
    assert attrs["pynchy.metadata_json"] == '{"slack_ts":"1.23"}'


def test_conversation_event_metadata_is_immutable_snapshot() -> None:
    metadata = {"slack_ts": "1.23"}
    event = _event(metadata=metadata)

    metadata["slack_ts"] = "4.56"

    assert event.span_attributes()["pynchy.metadata_json"] == '{"slack_ts":"1.23"}'
    mutable_metadata: Any = event.metadata
    with pytest.raises(TypeError):
        mutable_metadata["slack_ts"] = "7.89"


def test_conversation_event_metadata_deeply_snapshots_nested_values() -> None:
    metadata = {"nested": {"count": 1}, "items": ["alpha", {"beta": 2}]}
    event = _event(metadata=metadata)

    metadata["nested"]["count"] = 99
    metadata["items"].append("late")
    metadata["items"][1]["beta"] = 3

    assert (
        event.span_attributes()["pynchy.metadata_json"]
        == '{"items":["alpha",{"beta":2}],"nested":{"count":1}}'
    )


def test_conversation_event_metadata_exposes_deeply_immutable_values() -> None:
    event = _event(metadata={"nested": {"count": 1}, "items": ["alpha"]})
    metadata: Any = event.metadata

    with pytest.raises((TypeError, AttributeError)):
        metadata["nested"]["count"] = 2
    with pytest.raises((TypeError, AttributeError)):
        metadata["items"].append("late")


def test_conversation_event_span_attributes_include_full_values() -> None:
    event = _event(
        sender_name="Alice Example",
        content="hello\nthere",
        message_type="assistant",
        source_message_id="msg_1",
    )

    attrs = event.span_attributes()

    assert attrs["pynchy.message_type"] == "assistant"
    assert attrs["pynchy.content"] == "hello\nthere"
    assert attrs["pynchy.content_preview"] == "hello there"
    assert attrs["pynchy.sender_name"] == "Alice Example"
    assert attrs["pynchy.source_message_id"] == "msg_1"


def test_conversation_event_span_attributes_omit_empty_optional_values() -> None:
    attrs = _event().span_attributes()

    assert "pynchy.sender_name" not in attrs
    assert "pynchy.source_message_id" not in attrs
    assert "pynchy.metadata_json" not in attrs


def test_conversation_event_metadata_json_is_sorted_compact_and_stringifies() -> None:
    event = _event(metadata={"z": 2, "when": date(2026, 7, 10), "a": 1})

    assert event.span_attributes()["pynchy.metadata_json"] == '{"a":1,"when":"2026-07-10","z":2}'
