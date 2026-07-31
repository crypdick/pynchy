"""Reachable Discord application-command edge behavior."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from pynchy.config.api import DiscordConnectionConfig
from pynchy.plugins.channels.discord import DiscordChannel
from tests.test_discord_events import (
    DISCORD_BOT_ENV,
    _application_interaction,
)


def _channel(delivered: list[Any]) -> DiscordChannel:
    return DiscordChannel(
        "discord",
        DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV, group_policy="open"),
        "token",
        lambda _jid, message: delivered.append(message),
        lambda _jid, _timestamp, _chat_name: None,
        audio_cache_dir=Path("data/media/discord"),
    )


@pytest.mark.asyncio
async def test_application_command_preserves_thread_metadata_and_short_id():
    delivered: list[Any] = []
    channel = _channel(delivered)
    interaction = _application_interaction()
    interaction.channel.parent_id = "parent"
    interaction.channel.parent = MagicMock()
    interaction.channel.parent.name = "admin"

    await channel.events.handle_application_command(interaction, "approve", {"short_id": "js"})

    assert delivered[0].content == "/approve js"
    assert delivered[0].metadata["application_command"] == {
        "name": "approve",
        "options": {"short_id": "js"},
    }
    assert delivered[0].metadata["discord_parent_chat_jid"] == "discord:channel:parent"
    assert delivered[0].metadata["discord_parent_channel_name"] == "admin"
    interaction.response.send_message.assert_awaited_once_with(
        "✅ /approve js received", ephemeral=True
    )


@pytest.mark.asyncio
async def test_duplicate_application_command_and_response_failure_are_safe():
    delivered: list[Any] = []
    channel = _channel(delivered)
    interaction = _application_interaction()
    interaction.response.send_message = AsyncMock(
        side_effect=discord.DiscordException("interaction expired")
    )

    await channel.events.handle_application_command(interaction, "reset")
    await channel.events.handle_application_command(interaction, "reset")

    assert len(delivered) == 1
    interaction.response.send_message.assert_awaited_once_with("✅ /reset received", ephemeral=True)
