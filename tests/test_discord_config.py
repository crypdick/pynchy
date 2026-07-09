"""Tests for Discord connection config models.

Discord config mirrors the Slack connection shape
(``[connection.discord.<name>]`` with ``chat.<guild>`` subsections) but adds a
DM policy, an allowlist, and a nested ``channels`` map under each guild, since
one Discord guild channel can host many threads that inherit its config.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pynchy.config import Settings
from pynchy.config.models import (
    ConnectionsConfig,
    DiscordConnectionConfig,
    ProfileConfig,
    WorkspaceConfig,
)

DISCORD_BOT_ENV = "DISCORD_BOT_ENV"


def _profiles():
    return {"admin": ProfileConfig(is_admin=True)}


def test_minimal_connection_defaults():
    cfg = DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV)
    assert cfg.bot_token_env == DISCORD_BOT_ENV
    assert cfg.application_id is None
    assert cfg.dm_policy == "allowlist"
    assert cfg.allow_from == []
    assert cfg.group_policy == "allowlist"
    assert cfg.chat == {}


def test_dm_policy_rejects_unknown_value():
    with pytest.raises(ValidationError):
        DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV, dm_policy="sometimes")


def test_group_policy_rejects_unknown_value():
    with pytest.raises(ValidationError):
        DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV, group_policy="maybe")


def test_nested_guild_and_channel_config():
    cfg = DiscordConnectionConfig(
        bot_token_env=DISCORD_BOT_ENV,
        chat={
            "myguild": {
                "require_mention": True,
                "users": ["discord:1"],
                "roles": ["role:2"],
                "channels": {
                    "general": {
                        "enabled": True,
                        "require_mention": False,
                        "allow": ["some_tool"],
                        "deny": ["dangerous_tool"],
                    }
                },
            }
        },
    )
    guild = cfg.chat["myguild"]
    assert guild.require_mention is True
    assert guild.users == ["discord:1"]
    channel = guild.channels["general"]
    assert channel.require_mention is False
    assert channel.deny == ["dangerous_tool"]


def test_channel_require_mention_defaults_to_none_for_inheritance():
    # None means "inherit from the guild"; only an explicit bool overrides.
    cfg = DiscordConnectionConfig(
        bot_token_env=DISCORD_BOT_ENV, chat={"g": {"channels": {"c": {}}}}
    )
    assert cfg.chat["g"].channels["c"].require_mention is None


def test_connections_config_exposes_discord():
    conns = ConnectionsConfig(discord={"mybot": {"bot_token_env": DISCORD_BOT_ENV}})
    assert "mybot" in conns.discord
    assert conns.get_connection("discord", "mybot").bot_token_env == DISCORD_BOT_ENV


def test_discord_connection_ack_emoji_defaults_to_lobster():
    cfg = DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV)
    assert cfg.processing_ack_emoji == "🦞"


def test_discord_connection_ack_emoji_can_be_disabled():
    cfg = DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV, processing_ack_emoji=None)
    assert cfg.processing_ack_emoji is None


def test_get_connection_returns_none_for_missing_discord():
    conns = ConnectionsConfig()
    assert conns.get_connection("discord", "nope") is None


def test_settings_accept_discord_connection_with_workspace_profile():
    settings = Settings(
        connections={
            "mybot": {
                "type": "discord",
                "bot_token_env": "DISCORD_BOT_TOKEN",
                "dm_policy": "allowlist",
                "group_policy": "allowlist",
            }
        },
        profiles=_profiles(),
        workspaces={"discord-admin": WorkspaceConfig(profiles=["admin"])},
    )

    assert settings.workspaces["discord-admin"].profiles == ["admin"]


def test_settings_accept_discord_named_channel_config():
    settings = Settings(
        connections={
            "mybot": {
                "type": "discord",
                "bot_token_env": "DISCORD_BOT_TOKEN",
                "dm_policy": "allowlist",
                "group_policy": "allowlist",
            }
        },
        profiles=_profiles(),
        workspaces={"discord-admin": WorkspaceConfig(profiles=["admin"])},
    )

    assert settings.connections["mybot"].type == "discord"


def test_settings_accept_discord_dm_allowlist_with_workspace_profile():
    settings = Settings(
        connections={
            "mybot": {
                "type": "discord",
                "bot_token_env": "DISCORD_BOT_TOKEN",
                "dm_policy": "allowlist",
                "allow_from": ["discord:42"],
                "group_policy": "disabled",
            }
        },
        profiles=_profiles(),
        workspaces={"discord-dm": WorkspaceConfig(profiles=["admin"])},
    )

    assert settings.connections["mybot"].allow_from == ["discord:42"]


def test_settings_accept_discord_dm_name_allowlist_with_workspace_profile():
    settings = Settings(
        connections={
            "mybot": {
                "type": "discord",
                "bot_token_env": "DISCORD_BOT_TOKEN",
                "dm_policy": "allowlist",
                "allow_from": ["ricardo"],
                "group_policy": "disabled",
            }
        },
        profiles=_profiles(),
        workspaces={"discord-dm": WorkspaceConfig(profiles=["admin"])},
    )

    assert settings.connections["mybot"].allow_from == ["ricardo"]


def test_workspace_chat_ref_is_not_part_of_config_schema():
    with pytest.raises(ValidationError, match="Extra inputs"):
        WorkspaceConfig(profile="admin", chat="connection.discord.mybot.chat.123.channels.456")
