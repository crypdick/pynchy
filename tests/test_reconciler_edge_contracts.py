"""Focused public edge contracts for channel reconciliation."""

from __future__ import annotations

import asyncio

import pytest

from pynchy.host.orchestrator.messaging.reconciler import reconcile_all_channels, reset_cooldowns
from pynchy.host.orchestrator.messaging.sender import broadcast, finalize_stream_or_broadcast
from pynchy.plugins.api import NewMessage, OutboundEvent, OutboundEventType
from pynchy.state import (
    get_channel_cursor,
    get_pending_outbound,
    record_outbound_deliveries,
    set_channel_cursor,
    store_chat_metadata,
    store_message,
)
from pynchy.state.outbound import OutboundDelivery, OutboundDeliveryOperation
from tests.conftest import init_test_database
from tests.test_reconciler import TEST_GROUP, _make_channel, _make_deps


@pytest.fixture
async def _db():
    await init_test_database()
    await store_chat_metadata("group@g.us", "2024-01-01T00:00:00")


@pytest.fixture(autouse=True)
def _reset_reconciler_cooldowns():
    reset_cooldowns()
    yield
    reset_cooldowns()


@pytest.mark.usefixtures("_db")
@pytest.mark.asyncio
async def test_reconciler_skips_message_already_stored_in_canonical_chat():
    msg = NewMessage(
        id="msg-duplicate",
        chat_jid="group@g.us",
        sender="U1",
        sender_name="Alice",
        content="already stored",
        timestamp="2024-06-01T12:00:00",
    )
    await store_message(msg)
    remote = NewMessage(
        id=msg.id,
        chat_jid="slack:C123",
        sender=msg.sender,
        sender_name=msg.sender_name,
        content=msg.content,
        timestamp=msg.timestamp,
    )
    ch = _make_channel(inbound=[remote])
    deps = _make_deps(channels=[ch], workspaces={"group@g.us": TEST_GROUP})
    await set_channel_cursor("slack", "group@g.us", "inbound", "2024-01-01T00:00:00")

    await reconcile_all_channels(deps)

    deps.ingest_user_message.assert_not_awaited()
    deps.start_interactive_turn.assert_not_awaited()


@pytest.mark.usefixtures("_db")
@pytest.mark.asyncio
async def test_reconciler_retries_edit_as_post_when_channel_cannot_update_messages():
    await record_outbound_deliveries(
        "group@g.us",
        "accumulated trace",
        "agent_trace",
        [
            OutboundDelivery(
                channel_name="slack",
                operation=OutboundDeliveryOperation.EDIT,
                remote_message_id="message-123",
            )
        ],
    )
    ch = _make_channel()
    deps = _make_deps(channels=[ch], workspaces={"group@g.us": TEST_GROUP})

    await reconcile_all_channels(deps)

    ch.send_event.assert_awaited_once()
    assert not await get_pending_outbound("slack", "group@g.us")


@pytest.mark.usefixtures("_db")
@pytest.mark.asyncio
async def test_reconciler_does_not_move_outbound_cursor_backwards():
    await record_outbound_deliveries(
        "group@g.us",
        "older trace",
        "agent_trace",
        [OutboundDelivery(channel_name="slack")],
    )
    await set_channel_cursor("slack", "group@g.us", "outbound", "9999-01-01")
    ch = _make_channel()
    deps = _make_deps(channels=[ch], workspaces={"group@g.us": TEST_GROUP})

    await reconcile_all_channels(deps)

    assert await get_channel_cursor("slack", "group@g.us", "outbound") == "9999-01-01"


@pytest.mark.usefixtures("_db")
@pytest.mark.asyncio
async def test_reconciler_does_not_duplicate_an_outbound_send_in_progress():
    first_send_started = asyncio.Event()
    finish_first_send = asyncio.Event()
    ch = _make_channel()

    async def send_event(*_args: object) -> None:
        if ch.send_event.await_count == 1:
            first_send_started.set()
            await finish_first_send.wait()

    ch.send_event.side_effect = send_event
    deps = _make_deps(channels=[ch], workspaces={"group@g.us": TEST_GROUP})
    broadcast_task = asyncio.create_task(
        broadcast(
            deps,
            "group@g.us",
            OutboundEvent(type=OutboundEventType.TEXT, content="send once"),
        )
    )
    await first_send_started.wait()
    reconcile_task = asyncio.create_task(reconcile_all_channels(deps))

    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(reconcile_task), timeout=0.1)
        assert ch.send_event.await_count == 1
    finally:
        finish_first_send.set()
        await asyncio.gather(broadcast_task, reconcile_task, return_exceptions=True)

    assert ch.send_event.await_count == 1


@pytest.mark.usefixtures("_db")
@pytest.mark.asyncio
async def test_interrupted_stream_finalization_retries_the_existing_message():
    update_started = asyncio.Event()
    never_finish = asyncio.Event()
    ch = _make_channel()

    async def update_event(*_args: object) -> None:
        update_started.set()
        await never_finish.wait()

    ch.update_event = update_event
    deps = _make_deps(channels=[ch], workspaces={"group@g.us": TEST_GROUP})
    finalization = asyncio.create_task(
        finalize_stream_or_broadcast(
            deps,
            "group@g.us",
            OutboundEvent(type=OutboundEventType.RESULT, content="final answer"),
            {"slack": "stream-message-1"},
        )
    )
    await update_started.wait()
    finalization.cancel()
    with pytest.raises(asyncio.CancelledError):
        await finalization

    [pending] = await get_pending_outbound("slack", "group@g.us")
    assert pending.operation is OutboundDeliveryOperation.EDIT
    assert pending.remote_message_id == "stream-message-1"


@pytest.mark.usefixtures("_db")
@pytest.mark.asyncio
async def test_broadcast_keeps_a_retry_for_a_disconnected_channel():
    ch = _make_channel(connected=False)
    deps = _make_deps(channels=[ch], workspaces={"group@g.us": TEST_GROUP})

    delivered = await broadcast(
        deps,
        "group@g.us",
        OutboundEvent(type=OutboundEventType.RESULT, content="deliver after reconnect"),
    )

    assert delivered is False
    ch.send_event.assert_not_awaited()
    [pending] = await get_pending_outbound("slack", "group@g.us")
    assert pending.content == "deliver after reconnect"


@pytest.mark.usefixtures("_db")
@pytest.mark.asyncio
async def test_reconciler_sender_filter_rejection_skips_recovered_message(monkeypatch):
    msg = NewMessage(
        id="msg-rejected",
        chat_jid="slack:C123",
        sender="U04INTRUDER",
        sender_name="Intruder",
        content="blocked",
        timestamp="2024-06-01T00:00:00",
    )
    ch = _make_channel(inbound=[msg])
    deps = _make_deps(channels=[ch], workspaces={"group@g.us": TEST_GROUP})
    await set_channel_cursor("slack", "group@g.us", "inbound", "2024-01-01T00:00:00")
    monkeypatch.setattr(
        "pynchy.host.orchestrator.messaging.reconciler._allowed_message_filter",
        lambda *_args: [],
    )

    await reconcile_all_channels(deps)

    deps.ingest_user_message.assert_not_awaited()
    deps.start_interactive_turn.assert_not_awaited()


@pytest.mark.usefixtures("_db")
@pytest.mark.asyncio
async def test_reconciliation_fails_when_sender_policy_is_unconfigured(monkeypatch):
    ch = _make_channel(
        inbound=[
            NewMessage(
                id="msg-no-policy",
                chat_jid="slack:C123",
                sender="U1",
                sender_name="Alice",
                content="hello",
                timestamp="2024-06-01T00:00:00",
            )
        ]
    )
    deps = _make_deps(channels=[ch], workspaces={"group@g.us": TEST_GROUP})
    await set_channel_cursor("slack", "group@g.us", "inbound", "2024-01-01T00:00:00")
    monkeypatch.setattr(
        "pynchy.host.orchestrator.messaging.reconciler._allowed_message_filter",
        None,
    )

    with pytest.raises(RuntimeError, match="sender policy has not been configured"):
        await reconcile_all_channels(deps)
