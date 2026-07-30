"""Edge behavior for Discord outbound approval delivery."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pynchy.plugins.api import OutboundEvent, OutboundEventType
from tests.discord_channel_support import _channel, _FakeStreamChannel


@pytest.mark.asyncio
async def test_empty_approval_content_does_not_send_a_blank_discord_message() -> None:
    channel = _channel()
    channel.client = object()
    destination = _FakeStreamChannel()
    channel.resolve_channel = AsyncMock(return_value=destination)  # type: ignore[method-assign]

    await channel.send_event(
        "discord:channel:1",
        OutboundEvent(type=OutboundEventType.APPROVAL, content=""),
    )

    assert destination.sends == []
