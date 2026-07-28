"""Discord inbound access policy as observed through channel delivery."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pynchy.config.api import DiscordConnectionConfig
from pynchy.plugins.channels.discord import (
    DiscordAuthor,
    DiscordChannel,
    DiscordChannelDetails,
    DiscordInboundMessage,
)

if TYPE_CHECKING:
    from pynchy.types import NewMessage

BOT_ID = "999"
DISCORD_BOT_ENV = "X"


def _user(
    uid: str,
    *,
    bot: bool = False,
    roles: tuple[str, ...] = (),
    display_name: str | None = None,
    global_name: str | None = None,
    name: str | None = None,
) -> DiscordAuthor:
    return DiscordAuthor(
        id=uid,
        is_bot=bot,
        role_ids=frozenset(roles),
        display_name=display_name,
        global_name=global_name,
        name=name,
        rendered_name=name or display_name or uid,
    )


def _message(
    *,
    author: DiscordAuthor,
    guild_id: str | None,
    channel_id: str,
    guild_name: str | None = None,
    channel_name: str | None = None,
    parent_id: str | None = None,
    parent_name: str | None = None,
    mentions: tuple[str, ...] = (),
) -> DiscordInboundMessage:
    return DiscordInboundMessage(
        id=f"m-{author.id}-{channel_id}",
        author=author,
        guild_id=guild_id,
        guild_name=guild_name,
        channel=DiscordChannelDetails(
            id=channel_id,
            name=channel_name,
            parent_id=parent_id,
            parent_name=parent_name,
        ),
        content="hello",
        attachments=(),
        reply=None,
        forwarded_messages=(),
        mentioned_user_ids=frozenset(mentions),
        system_type=None,
        created_at=None,
    )


def _dm(
    author_id: str = "1",
    *,
    author_names: tuple[str, ...] = (),
    is_bot: bool = False,
) -> DiscordInboundMessage:
    return _message(
        author=_user(
            author_id,
            bot=is_bot,
            display_name=author_names[0] if author_names else None,
            name=author_names[-1] if author_names else None,
        ),
        guild_id=None,
        channel_id="dm-chan",
    )


def _guild(
    *,
    guild_id: str = "g1",
    guild_name: str | None = None,
    channel_id: str = "c1",
    channel_name: str | None = None,
    parent_channel_id: str | None = None,
    parent_channel_name: str | None = None,
    author_id: str = "u1",
    author_names: tuple[str, ...] = (),
    author_role_ids: tuple[str, ...] = (),
    mentions_bot: bool = False,
    is_bot: bool = False,
) -> DiscordInboundMessage:
    return _message(
        author=_user(
            author_id,
            bot=is_bot,
            roles=author_role_ids,
            display_name=author_names[0] if author_names else None,
            name=author_names[-1] if author_names else None,
        ),
        guild_id=guild_id,
        guild_name=guild_name,
        channel_id=channel_id,
        channel_name=channel_name,
        parent_id=parent_channel_id,
        parent_name=parent_channel_name,
        mentions=(BOT_ID,) if mentions_bot else (),
    )


async def _delivered_messages(
    msg: DiscordInboundMessage,
    *,
    workspaces: dict[str, object] | None = None,
    **cfg_kwargs: Any,
) -> list[tuple[str, NewMessage]]:
    delivered: list[tuple[str, NewMessage]] = []
    channel = DiscordChannel(
        "discord",
        DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV, **cfg_kwargs),
        "token",
        lambda jid, new_message: delivered.append((jid, new_message)),
        lambda _jid, _timestamp, _chat_name: None,
        audio_cache_dir=Path("data/media/discord"),
        workspaces=lambda: workspaces or {},
    )
    channel.bot_user_id = BOT_ID

    await channel.events.handle_inbound_message(msg)

    return delivered


async def _is_delivered(
    msg: DiscordInboundMessage,
    *,
    workspaces: dict[str, object] | None = None,
    **cfg_kwargs: Any,
) -> bool:
    return bool(await _delivered_messages(msg, workspaces=workspaces, **cfg_kwargs))


# --- bot filtering -----------------------------------------------------------


async def test_bot_author_denied_even_when_dm_open():
    assert await _is_delivered(_dm(is_bot=True), dm_policy="open") is False


# --- DM policy ---------------------------------------------------------------


async def test_dm_open_allows_anyone():
    assert await _is_delivered(_dm("777"), dm_policy="open") is True


async def test_dm_disabled_denies():
    assert await _is_delivered(_dm("1"), dm_policy="disabled") is False


async def test_dm_allowlist_allows_listed_user():
    assert await _is_delivered(_dm("1"), dm_policy="allowlist", allow_from=["discord:1"])


async def test_dm_allowlist_denies_unlisted_user():
    assert await _is_delivered(_dm("2"), dm_policy="allowlist", allow_from=["discord:1"]) is False


async def test_dm_allowlist_wildcard_allows_anyone():
    assert await _is_delivered(_dm("7"), dm_policy="allowlist", allow_from=["*"])


async def test_allow_from_accepts_bare_snowflake():
    assert await _is_delivered(_dm("1"), dm_policy="allowlist", allow_from=["1"])


async def test_dm_allowlist_accepts_human_user_name():
    assert await _is_delivered(
        _dm("1", author_names=("Alice", "asmith")),
        dm_policy="allowlist",
        allow_from=["alice"],
    )


# --- guild / group policy ----------------------------------------------------


async def test_group_disabled_denies_guild_message():
    assert await _is_delivered(_guild(mentions_bot=True), group_policy="disabled") is False


async def test_group_allowlist_denies_unconfigured_guild():
    assert await _is_delivered(_guild(mentions_bot=True), group_policy="allowlist") is False


async def test_group_allowlist_allows_registered_workspace_channel():
    assert await _is_delivered(
        _guild(channel_id="c1", mentions_bot=False),
        group_policy="allowlist",
        workspaces={"discord:channel:c1": object()},
    )


async def test_registered_workspace_never_bypasses_configured_member_or_role_auth():
    registered = {"discord:channel:runtime-thread": object()}
    for member_policy in (
        {"users": ["discord:allowed-user"]},
        {"roles": ["role:allowed-role"]},
    ):
        guild = {"g1": {"require_mention": False, **member_policy}}

        assert (
            await _is_delivered(
                _guild(channel_id="runtime-thread", author_id="intruder"),
                group_policy="allowlist",
                chat=guild,
                workspaces=registered,
            )
            is False
        )


async def test_group_open_allows_mentioned_message_in_unconfigured_guild():
    assert await _is_delivered(_guild(mentions_bot=True), group_policy="open")


async def test_group_open_denies_unmentioned_message_by_default():
    assert await _is_delivered(_guild(mentions_bot=False), group_policy="open") is False


# --- require_mention ---------------------------------------------------------


async def test_configured_guild_requires_mention_by_default():
    assert (
        await _is_delivered(
            _guild(mentions_bot=False),
            group_policy="allowlist",
            chat={"g1": {}},
        )
        is False
    )
    assert await _is_delivered(
        _guild(mentions_bot=True),
        group_policy="allowlist",
        chat={"g1": {}},
    )


async def test_channel_require_mention_false_overrides_guild():
    assert await _is_delivered(
        _guild(channel_id="c1", mentions_bot=False),
        group_policy="allowlist",
        chat={"g1": {"require_mention": True, "channels": {"c1": {"require_mention": False}}}},
    )


async def test_name_configured_guild_channel_allows_message():
    assert await _is_delivered(
        _guild(
            guild_id="123",
            guild_name="Synapse",
            channel_id="456",
            channel_name="code-improver",
            mentions_bot=False,
        ),
        group_policy="allowlist",
        chat={
            "synapse": {
                "name": "Synapse",
                "require_mention": True,
                "channels": {
                    "code-improver": {
                        "name": "code-improver",
                        "require_mention": False,
                    }
                },
            }
        },
    )


# --- channel enable / allowlist ----------------------------------------------


async def test_disabled_channel_denies():
    assert (
        await _is_delivered(
            _guild(channel_id="c1", mentions_bot=True),
            group_policy="allowlist",
            chat={"g1": {"channels": {"c1": {"enabled": False}}}},
        )
        is False
    )


async def test_channel_not_in_configured_allowlist_denies():
    assert (
        await _is_delivered(
            _guild(channel_id="c2", mentions_bot=True),
            group_policy="allowlist",
            chat={"g1": {"channels": {"c1": {}}}},
        )
        is False
    )


# --- threads inherit parent-channel config -----------------------------------


async def test_thread_inherits_parent_channel_config():
    assert await _is_delivered(
        _guild(channel_id="t1", parent_channel_id="c1", mentions_bot=False),
        group_policy="allowlist",
        chat={"g1": {"channels": {"c1": {"require_mention": False}}}},
    )


# --- member / role allowlists ------------------------------------------------


async def test_member_users_allowlist_permits_listed_sender():
    assert await _is_delivered(
        _guild(author_id="u1"),
        group_policy="allowlist",
        chat={"g1": {"require_mention": False, "users": ["discord:u1"]}},
    )


async def test_member_users_allowlist_permits_human_user_name():
    assert await _is_delivered(
        _guild(author_names=("Alice",)),
        group_policy="allowlist",
        chat={"g1": {"require_mention": False, "users": ["alice"]}},
    )


async def test_member_users_allowlist_denies_unlisted_sender():
    assert (
        await _is_delivered(
            _guild(author_id="u2"),
            group_policy="allowlist",
            chat={"g1": {"require_mention": False, "users": ["discord:u1"]}},
        )
        is False
    )


async def test_member_role_allowlist_permits_sender_with_matching_role():
    assert await _is_delivered(
        _guild(author_id="u9", author_role_ids=("r1",)),
        group_policy="allowlist",
        chat={"g1": {"require_mention": False, "roles": ["role:r1"]}},
    )
