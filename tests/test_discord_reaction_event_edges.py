"""Public Discord reaction event routing."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests.discord_channel_support import _channel


@pytest.mark.asyncio
async def test_guild_reaction_event_reaches_the_channel_callback() -> None:
    received: list[tuple[str, str, str, str]] = []
    channel = _channel()
    channel.on_reaction = lambda jid, message_id, user_id, emoji: received.append(
        (jid, message_id, user_id, emoji)
    )
    payload = MagicMock(
        user_id=42,
        guild_id=7,
        channel_id=8,
        message_id=9,
        emoji="👍",
    )

    await channel.events.handle_reaction(payload)

    assert received == [("discord:channel:8", "9", "42", "👍")]
