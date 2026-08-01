"""Public behavior for oversized Discord ask_user prompts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from pynchy.config.api import DiscordConnectionConfig
from pynchy.plugins.channels.discord import DiscordChannel

DISCORD_BOT_ENV = "DISCORD_TEST_TOKEN"
DISCORD_BOT_VALUE = "token"


class _FakeSendChannel:
    id = 101

    def __init__(self) -> None:
        self.sends: list[tuple[str, dict[str, object]]] = []

    async def send(self, content: str, **kwargs: object) -> object:
        self.sends.append((content, kwargs))
        return self


def _channel() -> DiscordChannel:
    return DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            dm_policy="open",
            group_policy="disabled",
        ).to_runtime_settings(),
        bot_token=DISCORD_BOT_VALUE,
        on_message=lambda _jid, _message: None,
        on_chat_metadata=lambda _jid, _timestamp, _name: None,
        audio_cache_dir=Path("data/media/discord"),
    )


@pytest.mark.asyncio
async def test_send_ask_user_sends_oversized_question_list_without_controls() -> None:
    channel = _channel()
    channel.client = object()
    target = _FakeSendChannel()
    channel.resolve_channel = AsyncMock(return_value=target)  # type: ignore[method-assign]
    questions = [{"question": f"Question {index}", "options": []} for index in range(5)]

    message_id = await channel.send_ask_user("discord:direct:42", "request-1", questions)

    assert message_id == "discord-101"
    assert target.sends[0][1]["view"] is None
