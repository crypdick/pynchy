"""Tests for Discord ↔ pynchy JID conversion helpers.

Discord jids are ``discord:<kind>:<snowflake>`` where kind is
``direct`` (DM, keyed on the user snowflake), ``channel`` (guild channel
or thread, keyed on the channel snowflake), or ``group`` (group DM).
"""

from __future__ import annotations

import pytest

from pynchy.plugins.channels.discord import (
    JID_PREFIX,
    DiscordJid,
    channel_jid,
    dm_jid,
    group_jid,
    is_discord_jid,
    parse_jid,
    snowflake_of,
)


def test_dm_jid_keys_off_user_snowflake():
    assert dm_jid(123) == "discord:direct:123"


def test_channel_jid_keys_off_channel_snowflake():
    assert channel_jid(456) == "discord:channel:456"


def test_group_jid_keys_off_channel_snowflake():
    assert group_jid(789) == "discord:group:789"


def test_jid_builders_accept_str_ids():
    assert dm_jid("123") == "discord:direct:123"
    assert channel_jid("456") == "discord:channel:456"


def test_prefix_constant():
    assert JID_PREFIX == "discord:"
    assert dm_jid(1).startswith(JID_PREFIX)


def test_parse_roundtrip_dm():
    assert parse_jid(dm_jid(123)) == DiscordJid(kind="direct", snowflake="123")


def test_parse_roundtrip_channel():
    assert parse_jid(channel_jid(456)) == DiscordJid(kind="channel", snowflake="456")


def test_parse_roundtrip_group():
    assert parse_jid(group_jid(789)) == DiscordJid(kind="group", snowflake="789")


def test_snowflake_of_extracts_id_regardless_of_kind():
    assert snowflake_of(dm_jid(123)) == "123"
    assert snowflake_of(channel_jid(456)) == "456"


def test_is_discord_jid_true_for_discord_jids():
    assert is_discord_jid(dm_jid(1)) is True
    assert is_discord_jid(channel_jid(1)) is True


def test_is_discord_jid_false_for_other_channels():
    assert is_discord_jid("slack:C123") is False
    assert is_discord_jid("") is False


def test_parse_rejects_non_discord_jid():
    with pytest.raises(ValueError, match="not a Discord jid"):
        parse_jid("slack:C123")


def test_parse_rejects_unknown_kind():
    with pytest.raises(ValueError, match="unknown Discord jid kind"):
        parse_jid("discord:banana:123")
