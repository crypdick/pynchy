"""Public behavior for ask_user controls detached from their Discord view."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pynchy.config.api import DiscordConnectionConfig
from pynchy.plugins.channels.discord import DiscordChannel
from tests.test_discord_events import DISCORD_BOT_ENV


def _channel() -> DiscordChannel:
    return DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV, dm_policy="open", group_policy="disabled"
        ).to_runtime_settings(),
        bot_token=DISCORD_BOT_ENV,
        on_message=lambda _jid, _message: None,
        on_chat_metadata=lambda _jid, _timestamp, _name: None,
        audio_cache_dir=Path("data/media/discord"),
    )


def _interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.response.send_modal = AsyncMock()
    return interaction


@pytest.mark.asyncio
async def test_file_upload_button_rejects_an_unattached_view():
    channel = _channel()
    channel.client = object()
    target = MagicMock()
    target.send = AsyncMock()
    sent_message = MagicMock()
    sent_message.id = 101
    target.send.return_value = sent_message
    channel.resolve_channel = AsyncMock(return_value=target)  # type: ignore[method-assign]
    questions = [
        {
            "header": "Evidence",
            "question": "Upload a screenshot of the failure.",
            "fileUpload": {"required": False, "maxFiles": 2},
        }
    ]

    await channel.send_ask_user("discord:direct:42", "request-1", questions)

    view = target.send.call_args.kwargs["view"]
    button = next(item for item in view.children if item.label == "Attach files")
    view.remove_item(button)

    with pytest.raises(RuntimeError, match="before the view was attached"):
        await button.callback(_interaction())
