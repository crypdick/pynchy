from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.conversation.events import ConversationEvent, ConversationEventKind
from pynchy.conversation.models import (
    ConversationClaimId,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalDeliveryReceipt,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.identifiers import GroupFolder
from pynchy.state import (
    admit_conversation_delivery,
    admit_external_delivery_receipt,
    claim_next_conversation_delivery,
    complete_conversation_delivery,
    get_chat_history,
    get_conversation_event_pointers_since,
    get_messages_since,
    init_test_database,
    store_chat_metadata,
    store_conversation_event_pointer,
    store_message_direct,
)
from pynchy.state.conversation_events import ConversationEventRef


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


async def test_store_and_load_legacy_projection_pointer() -> None:
    await init_test_database()
    event = _event("evt_1", "2026-07-10T00:00:00+00:00")
    await store_conversation_event_pointer(
        event,
        ConversationEventRef("evt_1", "legacy:event:evt_1"),
    )

    rows = await get_conversation_event_pointers_since("slack:C123", None)

    assert len(rows) == 1
    assert rows[0]["event_id"] == "evt_1"
    assert rows[0]["content_preview"] == "body evt_1"
    assert rows[0]["trace_ref"] == "legacy:event:evt_1"
    assert rows[0]["metadata"] == {"source": "test"}


async def test_store_projection_pointer_defaults_missing_metadata_to_empty_object() -> None:
    await init_test_database()
    event = ConversationEvent(
        event_id="evt_empty_metadata",
        turn_id="turn_1",
        chat_jid="slack:C123",
        timestamp="2026-07-10T00:00:00+00:00",
        kind=ConversationEventKind.USER_MESSAGE,
        sender="alice",
        sender_name="Alice",
        content="body",
        message_type="user",
        metadata={},
    )

    await store_conversation_event_pointer(
        event,
        ConversationEventRef("evt_empty_metadata", "legacy:event:evt_empty_metadata"),
    )

    rows = await get_conversation_event_pointers_since("slack:C123", None)

    assert rows[0]["metadata"] == {}


async def test_since_filter_is_exclusive() -> None:
    await init_test_database()
    await store_conversation_event_pointer(
        _event("evt_1", "2026-07-10T00:00:00+00:00"),
        ConversationEventRef("evt_1", "legacy:event:evt_1"),
    )
    await store_conversation_event_pointer(
        _event("evt_2", "2026-07-10T00:01:00+00:00"),
        ConversationEventRef("evt_2", "legacy:event:evt_2"),
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
        ConversationEventRef("evt_1", "legacy:event:evt_1"),
    )

    rows = await get_conversation_event_pointers_since("slack:C123", None)

    assert rows[0]["metadata"] == {
        "source": "test",
        "nested": {"items": ["one", {"two": 2}]},
    }


async def test_get_messages_since_ignores_projected_conversation_events() -> None:
    await init_test_database()
    await store_conversation_event_pointer(
        _event("evt_1", "2026-07-10T00:00:00+00:00"),
        ConversationEventRef("evt_1", "legacy:event:evt_1"),
    )

    messages = await get_messages_since("slack:C123", "")

    assert messages == []


async def test_get_messages_since_reads_sqlite_body_when_projection_exists() -> None:
    await init_test_database()
    await store_chat_metadata("slack:C123", "2026-07-10T00:00:00+00:00")
    sqlite_body = "line one\n" + ("x " * 700) + "\nline after preview"
    await store_message_direct(
        message_id="source_long",
        chat_jid="slack:C123",
        sender="alice",
        sender_name="Alice",
        content=sqlite_body,
        timestamp="2026-07-10T00:00:00+00:00",
        is_from_me=False,
    )
    await store_conversation_event_pointer(
        _event(
            "evt_long",
            "2026-07-10T00:00:00+00:00",
            content="preview only",
            source_message_id="source_long",
        ),
        ConversationEventRef("evt_long", "legacy:event:evt_long"),
    )

    messages = await get_messages_since("slack:C123", "")

    assert len(messages) == 1
    assert len(messages[0].content) > 500
    assert messages[0].id == "source_long"
    assert messages[0].content == sqlite_body


async def test_get_messages_since_reads_sqlite_rows_after_rollback() -> None:
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
        ConversationEventRef("evt_1", "legacy:event:evt_1"),
    )

    messages = await get_messages_since("slack:C123", "")

    assert [message.id for message in messages] == ["legacy_1"]
    assert [message.content for message in messages] == ["legacy body"]


async def test_get_messages_since_excludes_retired_claim_projection() -> None:
    await init_test_database()
    subject = ConversationSubject(
        namespace=ConversationSubjectNamespace("linear:team:issue"),
        key=ConversationSubjectKey("SYN-1"),
    )

    async def admit(delivery_id: str):
        identity = ExternalDeliveryIdentity(
            provider=ExternalProvider("linear"),
            route=ExternalRoute("team"),
            delivery_id=ExternalDeliveryId(delivery_id),
        )
        await admit_external_delivery_receipt(
            ExternalDeliveryReceipt(
                identity=identity,
                payload_sha256=f"sha-{delivery_id}",
                received_at="2026-07-10T00:00:00+00:00",
            )
        )
        return await admit_conversation_delivery(identity, subject, GroupFolder("triage"))

    first = await admit("delivery-1")
    second = await admit("delivery-2")
    first_claim = ConversationClaimId("claim-1")
    assert await claim_next_conversation_delivery(first.conversation.id, first_claim)
    await store_message_direct(
        message_id="delivery-1",
        chat_jid="slack:C123",
        sender="linear",
        sender_name="Linear",
        content="old projection",
        timestamp="2026-07-10T00:00:01+00:00",
        is_from_me=False,
        metadata={
            "conversation_id": first.conversation.id,
            "conversation_claim_id": first_claim,
        },
    )
    assert await complete_conversation_delivery(first_claim)

    second_claim = ConversationClaimId("claim-2")
    assert await claim_next_conversation_delivery(second.conversation.id, second_claim)
    await store_message_direct(
        message_id="delivery-2",
        chat_jid="slack:C123",
        sender="linear",
        sender_name="Linear",
        content="current projection",
        timestamp="2026-07-10T00:00:02+00:00",
        is_from_me=False,
        metadata={
            "conversation_id": second.conversation.id,
            "conversation_claim_id": second_claim,
        },
    )

    pending = await get_messages_since("slack:C123", "")
    history = await get_chat_history("slack:C123")

    assert [message.id for message in pending] == ["delivery-2"]
    assert [message.id for message in history] == ["delivery-1", "delivery-2"]


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
        ConversationEventRef("evt_1", "legacy:event:evt_1"),
    )

    messages = await get_messages_since("slack:C123", "")

    assert messages == []


async def test_get_messages_since_excludes_projected_system_notices() -> None:
    await init_test_database()
    await store_conversation_event_pointer(
        ConversationEvent(
            event_id="evt_1",
            turn_id="turn_1",
            chat_jid="slack:C123",
            timestamp="2026-07-10T00:00:00+00:00",
            kind=ConversationEventKind.SYSTEM_NOTICE,
            sender="system_notice",
            sender_name="System",
            content="[System Notice] body",
            message_type="user",
            metadata={"source": "test"},
        ),
        ConversationEventRef("evt_1", "legacy:event:evt_1"),
    )

    messages = await get_messages_since("slack:C123", "")

    assert messages == []


async def test_store_projection_pointer_rejects_mismatched_ref() -> None:
    await init_test_database()
    event = _event("evt_1", "2026-07-10T00:00:00+00:00")

    with pytest.raises(
        ValueError,
        match="Conversation ref event_id 'evt_2' does not match event 'evt_1'",
    ):
        await store_conversation_event_pointer(
            event,
            ConversationEventRef("evt_2", "legacy:event:evt_2"),
        )

    rows = await get_conversation_event_pointers_since("slack:C123", None)
    assert rows == []


async def test_reader_returns_empty_metadata_for_malformed_persisted_json() -> None:
    """A corrupt historical row is safely represented through the public reader."""
    cursor = MagicMock()
    cursor.fetchall = AsyncMock(return_value=[{"metadata": "{not valid json"}])
    database = MagicMock()
    database.execute = AsyncMock(return_value=cursor)

    with patch("pynchy.state.conversation_events._get_db", return_value=database):
        rows = await get_conversation_event_pointers_since("slack:C123", None)

    assert rows == [{"metadata": {}}]


async def test_reader_returns_empty_metadata_for_missing_persisted_json() -> None:
    cursor = MagicMock()
    cursor.fetchall = AsyncMock(return_value=[{"metadata": None}])
    database = MagicMock()
    database.execute = AsyncMock(return_value=cursor)

    with patch("pynchy.state.conversation_events._get_db", return_value=database):
        rows = await get_conversation_event_pointers_since("slack:C123", None)

    assert rows == [{"metadata": {}}]
