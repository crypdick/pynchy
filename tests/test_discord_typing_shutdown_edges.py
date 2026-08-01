"""Public Discord typing cleanup during channel shutdown."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from tests.discord_channel_support import _channel, _FakeTypingChannel


@pytest.mark.asyncio
async def test_disconnect_cancels_all_active_typing_refreshes() -> None:
    channel = _channel()
    channel.client = object()
    typing_channel = _FakeTypingChannel()
    channel.resolve_channel = AsyncMock(return_value=typing_channel)  # type: ignore[method-assign]
    channel.voice.disconnect = AsyncMock()
    channel.lifecycle.disconnect = AsyncMock()

    await channel.set_typing("discord:channel:1", is_typing=True)
    await channel.set_typing("discord:channel:2", is_typing=True)
    await asyncio.sleep(0)
    calls_before_disconnect = typing_channel.typing_calls

    await channel.disconnect()
    await asyncio.sleep(0)

    assert typing_channel.typing_calls == calls_before_disconnect
    channel.voice.disconnect.assert_awaited_once()
    channel.lifecycle.disconnect.assert_awaited_once()
