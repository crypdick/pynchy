"""Public Discord group-provisioning behavior."""

from __future__ import annotations

import pytest

from pynchy.config.api import DiscordConnectionConfig
from tests.discord_channel_support import (
    DISCORD_BOT_ENV,
    _channel,
    _FakeDiscordClient,
    _FakeDiscordGuild,
    _FakeDiscordTextChannel,
)


@pytest.mark.asyncio
async def test_create_group_requires_a_connected_client():
    with pytest.raises(RuntimeError, match="client is not connected"):
        await _channel().create_group("System Review")


@pytest.mark.asyncio
async def test_create_group_reuses_an_existing_workspace_channel():
    ch = _channel()
    guild = _FakeDiscordGuild(123, "Synapse", [_FakeDiscordTextChannel(456, "system-review")])
    ch.client = _FakeDiscordClient([guild])

    assert await ch.create_group("System Review") == "discord:channel:456"
    assert guild.created == []


@pytest.mark.asyncio
async def test_create_group_reuses_an_existing_configured_channel():
    ch = _channel(
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            group_policy="allowlist",
            chat={"synapse": {"channels": {"review": {}}}},
        )
    )
    guild = _FakeDiscordGuild(123, "synapse", [_FakeDiscordTextChannel(456, "review")])
    ch.client = _FakeDiscordClient([guild])

    assert await ch.create_group("synapse.channels.review") == "discord:channel:456"
    assert guild.created == []


@pytest.mark.asyncio
async def test_create_group_rejects_an_unconfigured_channel_ref():
    ch = _channel(
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            group_policy="allowlist",
            chat={"synapse": {"channels": {}}},
        )
    )
    ch.client = object()

    with pytest.raises(ValueError, match="not a configured guild channel"):
        await ch.create_group("synapse.channels.missing")


@pytest.mark.asyncio
async def test_create_group_reports_a_missing_configured_guild():
    ch = _channel(
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            group_policy="allowlist",
            chat={"synapse": {"channels": {"review": {}}}},
        )
    )
    ch.client = _FakeDiscordClient([])

    with pytest.raises(RuntimeError, match="guild not found"):
        await ch.create_group("synapse.channels.review")


@pytest.mark.asyncio
async def test_create_group_rejects_workspace_provisioning_when_groups_are_disabled():
    ch = _channel(
        config=DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV, group_policy="disabled")
    )
    ch.client = _FakeDiscordClient([_FakeDiscordGuild(123, "Synapse", [])])

    with pytest.raises(ValueError, match="guild messages are disabled"):
        await ch.create_group("System Review")


@pytest.mark.asyncio
async def test_create_group_rejects_multiple_configured_guilds_for_workspace_provisioning():
    ch = _channel(
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            group_policy="allowlist",
            chat={"one": {}, "two": {}},
        )
    )
    ch.client = _FakeDiscordClient([])

    with pytest.raises(RuntimeError, match="Multiple Discord guilds are configured"):
        await ch.create_group("System Review")


@pytest.mark.asyncio
async def test_create_group_reports_when_bot_has_no_workspace_guild():
    ch = _channel()
    ch.client = _FakeDiscordClient([])

    with pytest.raises(RuntimeError, match="not in any guild"):
        await ch.create_group("System Review")


@pytest.mark.asyncio
async def test_create_group_reports_missing_configured_workspace_guild():
    ch = _channel(
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            group_policy="allowlist",
            chat={"synapse": {}},
        )
    )
    ch.client = _FakeDiscordClient([])

    with pytest.raises(RuntimeError, match="Configured Discord guild not found"):
        await ch.create_group("System Review")


@pytest.mark.asyncio
async def test_create_group_uses_the_single_configured_workspace_guild():
    ch = _channel(
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            group_policy="allowlist",
            chat={"synapse": {}},
        )
    )
    guild = _FakeDiscordGuild(123, "Synapse", [])
    ch.client = _FakeDiscordClient([guild])

    assert await ch.create_group("System Review") == "discord:channel:789"
    assert guild.created == ["system-review"]


@pytest.mark.asyncio
async def test_create_group_rejects_ambiguous_workspace_guilds():
    ch = _channel()
    ch.client = _FakeDiscordClient(
        [_FakeDiscordGuild(123, "One", []), _FakeDiscordGuild(456, "Two", [])]
    )

    with pytest.raises(RuntimeError, match="Multiple Discord guilds are available"):
        await ch.create_group("System Review")
