"""Public streaming behavior for delivery boundaries and shutdown."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.host.orchestrator.messaging.streaming import (
    OutputDeps,
    StreamState,
    TraceBatcher,
    stream_text_to_channels,
)
from pynchy.plugins.api import Channel, OutboundEvent, OutboundEventType


def _channel(name: str) -> MagicMock:
    channel = MagicMock(spec=Channel)
    channel.name = name
    channel.is_connected.return_value = True
    channel.owns_jid.return_value = True
    channel.post_event = AsyncMock(return_value=f"{name}-message")
    channel.update_event = AsyncMock()
    return channel


def _deps(*channels: MagicMock) -> MagicMock:
    deps = MagicMock(spec=OutputDeps)
    deps.channels = list(channels)
    return deps


def _state(content: str = "hello") -> StreamState:
    return StreamState(event=OutboundEvent(type=OutboundEventType.TEXT, content=content))


@pytest.mark.asyncio
async def test_streaming_suppresses_throttled_updates() -> None:
    channel = _channel("chat")
    state = _state()
    state.last_update = 10.0

    with patch(
        "pynchy.host.orchestrator.messaging.streaming.time.monotonic",
        return_value=10.1,
    ):
        await stream_text_to_channels(_deps(channel), "chat:1", state)

    channel.post_event.assert_not_awaited()
    channel.update_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_streaming_ignores_channels_that_cannot_receive_updates() -> None:
    disconnected = _channel("disconnected")
    disconnected.is_connected.return_value = False
    unowned = _channel("unowned")
    unowned.owns_jid.return_value = False
    deps = _deps(disconnected, unowned)

    await stream_text_to_channels(deps, "chat:1", _state(), final=True)

    disconnected.post_event.assert_not_awaited()
    unowned.post_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_streaming_delivery_failure_does_not_escape_channel_boundary() -> None:
    channel = _channel("chat")
    channel.update_event.side_effect = OSError("edit failed")
    state = _state()
    state.message_ids["chat"] = "message-1"

    await stream_text_to_channels(_deps(channel), "chat:1", state, final=True)

    channel.update_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_trace_batcher_flush_all_closes_every_buffered_chat() -> None:
    channel = _channel("chat")
    batcher = TraceBatcher(_deps(channel), cooldown=999.0)
    batcher.enqueue("chat:1", OutboundEvent(type=OutboundEventType.TEXT, content="one"))
    batcher.enqueue("chat:2", OutboundEvent(type=OutboundEventType.TEXT, content="two"))

    await batcher.flush_all()

    assert [call.args[0] for call in channel.post_event.await_args_list] == ["chat:1", "chat:2"]


@pytest.mark.asyncio
async def test_trace_batcher_cancel_discards_pending_timer() -> None:
    channel = _channel("chat")
    batcher = TraceBatcher(_deps(channel), cooldown=0.01)
    batcher.enqueue("chat:1", OutboundEvent(type=OutboundEventType.TEXT, content="one"))

    batcher.cancel()
    await asyncio.sleep(0.02)

    channel.post_event.assert_not_awaited()
