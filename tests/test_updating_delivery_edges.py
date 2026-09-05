"""Public updating delivery behavior at channel and ledger boundaries."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pynchy.host.orchestrator.messaging import sender
from pynchy.host.orchestrator.messaging.sender import (
    UpdatingMessage,
    deliver_updating_event,
)
from pynchy.plugins.api import Channel, OutboundEvent, OutboundEventType
from pynchy.state.api import OutboundDeliveryOperation, get_pending_outbound
from tests.conftest import init_test_database


def _event(content: str = "delta") -> OutboundEvent:
    return OutboundEvent(type=OutboundEventType.TEXT, content=content)


def _channel(name: str) -> MagicMock:
    channel = MagicMock(spec=Channel)
    channel.name = name
    channel.is_connected.return_value = True
    channel.owns_jid.return_value = True
    channel.post_event = AsyncMock(return_value=f"{name}-message")
    channel.update_event = AsyncMock()
    channel.send_event = AsyncMock()
    return channel


def _deps(*channels: MagicMock) -> MagicMock:
    deps = MagicMock()
    deps.channels = list(channels)
    return deps


@pytest.mark.asyncio
async def test_updating_delivery_skips_disconnected_and_unowned_channels() -> None:
    disconnected = _channel("disconnected")
    disconnected.is_connected.return_value = False
    unowned = _channel("unowned")
    unowned.owns_jid.return_value = False

    messages = {"existing": UpdatingMessage("msg", "old")}
    result = await deliver_updating_event(
        _deps(disconnected, unowned),
        "chat:1",
        _event(),
        messages,
        source="test",
    )

    assert result == messages
    disconnected.post_event.assert_not_awaited()
    unowned.post_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_updating_delivery_falls_back_from_post_to_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _channel("chat")
    channel.post_event.side_effect = OSError("post failed")
    record = AsyncMock(return_value=7)
    mark = AsyncMock()
    monkeypatch.setattr(sender.state, "record_outbound_deliveries", record)
    monkeypatch.setattr(sender.state, "mark_delivery_succeeded", mark)

    result = await deliver_updating_event(_deps(channel), "chat:1", _event(), {}, source="test")

    assert result == {}
    channel.send_event.assert_awaited_once()
    mark.assert_awaited_once_with(7, "chat", OutboundDeliveryOperation.POST, None)


@pytest.mark.asyncio
async def test_updating_delivery_handles_falsey_post_and_failed_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _channel("chat")
    channel.post_event.return_value = None
    channel.send_event.side_effect = OSError("send failed")
    record = AsyncMock(side_effect=[None, 8])
    mark_error = AsyncMock(side_effect=RuntimeError("ledger unavailable"))
    monkeypatch.setattr(sender.state, "record_outbound_deliveries", record)
    monkeypatch.setattr(sender.state, "mark_delivery_error", mark_error)

    first = await deliver_updating_event(
        _deps(channel), "chat:1", _event("first"), {}, source="test"
    )
    second = await deliver_updating_event(
        _deps(channel), "chat:1", _event("second"), {}, source="test"
    )

    assert first == {}
    assert second == {}
    assert channel.post_event.await_count == 2
    assert channel.send_event.await_count == 2
    mark_error.assert_awaited_once_with(8, "chat", "send failed")


@pytest.mark.asyncio
async def test_updating_delivery_keeps_updated_message_when_ledger_mark_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _channel("chat")
    mark = AsyncMock(side_effect=RuntimeError("ledger unavailable"))
    monkeypatch.setattr(sender.state, "record_outbound_deliveries", AsyncMock(return_value=9))
    monkeypatch.setattr(sender.state, "mark_delivery_succeeded", mark)

    result = await deliver_updating_event(
        _deps(channel),
        "chat:1",
        _event("new"),
        {"chat": UpdatingMessage("message-1", "old")},
        source="test",
    )

    assert result["chat"] == UpdatingMessage("message-1", "old\nnew")
    channel.update_event.assert_awaited_once()
    mark.assert_awaited_once_with(9, "chat", OutboundDeliveryOperation.EDIT, "message-1")


@pytest.mark.asyncio
async def test_failed_update_and_fallback_keep_anchor_and_pending_content() -> None:
    await init_test_database()
    channel = _channel("chat")
    channel.update_event.side_effect = OSError("edit failed")
    channel.post_event.side_effect = OSError("post failed")
    channel.send_event.side_effect = OSError("send failed")
    messages = {"chat": UpdatingMessage("message-1", "old")}

    result = await deliver_updating_event(
        _deps(channel), "chat:1", _event("new"), messages, source="test"
    )

    assert result == {"chat": UpdatingMessage("message-1", "old")}
    assert messages == result
    channel.send_event.assert_awaited_once()
    [pending] = await get_pending_outbound("chat", "chat:1")
    assert pending.content == "old\nnew"
    assert pending.operation is OutboundDeliveryOperation.EDIT
    assert pending.remote_message_id == "message-1"
