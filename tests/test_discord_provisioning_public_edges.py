"""Public Discord group-provisioning behavior."""

from __future__ import annotations

import pytest

from pynchy.config.api import DiscordConnectionConfig
from tests.discord_channel_support import (
    DISCORD_BOT_ENV,
    _channel,
    _FakeDiscordClient,
    _FakeDiscordForum,
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
async def test_resolve_chat_jid_provisions_and_reuses_a_configured_forum():
    ch = _channel(
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            group_policy="allowlist",
            chat={
                "synapse": {
                    "channels": {
                        "systems": {
                            "name": "Systems",
                            "kind": "forum",
                            "category": "Systems",
                        }
                    }
                }
            },
        )
    )
    guild = _FakeDiscordGuild(123, "synapse", [])
    ch.client = _FakeDiscordClient([guild])

    first = await ch.resolve_chat_jid("synapse.channels.systems")
    guild.forums[0].available_tags = guild.forums[0].available_tags[:1]
    guild.forums[0].category_id = None
    second = await ch.resolve_chat_jid("synapse.channels.systems")

    assert first == second == "discord:channel:891"
    assert [category.name for category in guild.categories] == ["Systems"]
    assert [forum.name for forum in guild.forums] == ["systems"]
    assert [tag.name for tag in guild.forums[0].available_tags] == [
        "issue",
        "automation",
        "planning",
        "testing",
        "topic",
    ]
    assert guild.forums[0].category_id == guild.categories[0].id
    assert await ch.resolve_chat_jid("synapse.channels.systems") == second


@pytest.mark.asyncio
async def test_resolve_chat_jid_provisions_forum_without_category():
    ch = _channel(
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            group_policy="allowlist",
            chat={
                "synapse": {
                    "channels": {"systems": {"kind": "forum"}},
                }
            },
        )
    )
    guild = _FakeDiscordGuild(123, "synapse", [])
    ch.client = _FakeDiscordClient([guild])

    assert await ch.resolve_chat_jid("synapse.channels.systems") == "discord:channel:891"
    assert guild.categories == []
    guild.forums[0].guild = None
    assert await ch.resolve_chat_jid("synapse.channels.systems") == "discord:channel:891"


@pytest.mark.asyncio
async def test_resolve_chat_jid_rejects_uneditable_forum_reconciliation(monkeypatch):
    ch = _channel(
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV,
            group_policy="allowlist",
            chat={"synapse": {"channels": {"systems": {"kind": "forum"}}}},
        )
    )
    guild = _FakeDiscordGuild(123, "synapse", [])
    ch.client = _FakeDiscordClient([guild])
    await ch.resolve_chat_jid("synapse.channels.systems")
    guild.forums[0].available_tags = []
    monkeypatch.setattr(_FakeDiscordForum, "edit", None)

    with pytest.raises(TypeError, match="configuration reconciliation"):
        await ch.resolve_chat_jid("synapse.channels.systems")


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
