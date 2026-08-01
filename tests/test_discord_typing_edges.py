"""Public Discord typing guard behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tests.discord_channel_support import _channel, _configured_voice_channel


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel", "jid"),
    [
        (_channel(), "discord:channel:1"),
        (_configured_voice_channel(), "discord:voice:2"),
    ],
)
async def test_set_typing_ignores_unavailable_or_voice_destinations(channel, jid: str) -> None:
    channel.client = object()
    if jid.endswith("channel:1"):
        channel.client = None
    channel.resolve_channel = AsyncMock()  # type: ignore[method-assign]

    await channel.set_typing(jid, is_typing=True)

    channel.resolve_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_stopping_typing_without_an_active_refresh_is_idempotent() -> None:
    channel = _channel()
    channel.client = object()

    await channel.set_typing("discord:channel:1", is_typing=False)
