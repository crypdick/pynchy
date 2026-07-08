"""Tests for the Discord inbound access-control decision tree.

``DiscordAccess.decide`` is a pure function over an ``InboundContext`` (fields
already extracted from a ``discord.Message``) plus the connection config. It
returns ``"allow"``, ``"deny"``, or ``"pairing"``. v1 has no pairing flow, so
the DM-not-on-allowlist case denies; the ``"pairing"`` value is reserved so a
pairing collaborator can slot in later without changing callers.
"""

from __future__ import annotations

from pynchy.config.models import DiscordConnectionConfig
from pynchy.plugins.channels.discord._access import DiscordAccess, InboundContext


def _dm(
    author_id: str = "1",
    *,
    author_names: frozenset[str] = frozenset(),
    is_bot: bool = False,
) -> InboundContext:
    return InboundContext(
        is_dm=True,
        author_id=author_id,
        author_is_bot=is_bot,
        guild_id=None,
        guild_name=None,
        channel_id="dm-chan",
        channel_name=None,
        parent_channel_id=None,
        parent_channel_name=None,
        author_role_ids=frozenset(),
        mentions_bot=False,
        author_names=author_names,
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
    author_names: frozenset[str] = frozenset(),
    author_role_ids: frozenset[str] = frozenset(),
    mentions_bot: bool = False,
    is_bot: bool = False,
) -> InboundContext:
    return InboundContext(
        is_dm=False,
        author_id=author_id,
        author_is_bot=is_bot,
        guild_id=guild_id,
        guild_name=guild_name,
        channel_id=channel_id,
        channel_name=channel_name,
        parent_channel_id=parent_channel_id,
        parent_channel_name=parent_channel_name,
        author_role_ids=author_role_ids,
        mentions_bot=mentions_bot,
        author_names=author_names,
    )


def _access(**cfg_kwargs) -> DiscordAccess:
    return DiscordAccess(DiscordConnectionConfig(bot_token_env="X", **cfg_kwargs))


# --- bot filtering -----------------------------------------------------------


def test_bot_author_denied_even_when_dm_open():
    access = _access(dm_policy="open")
    assert access.decide(_dm(is_bot=True)) == "deny"


# --- DM policy ---------------------------------------------------------------


def test_dm_open_allows_anyone():
    assert _access(dm_policy="open").decide(_dm("999")) == "allow"


def test_dm_disabled_denies():
    assert _access(dm_policy="disabled").decide(_dm("1")) == "deny"


def test_dm_allowlist_allows_listed_user():
    access = _access(dm_policy="allowlist", allow_from=["discord:1"])
    assert access.decide(_dm("1")) == "allow"


def test_dm_allowlist_denies_unlisted_user():
    access = _access(dm_policy="allowlist", allow_from=["discord:1"])
    assert access.decide(_dm("2")) == "deny"


def test_dm_allowlist_wildcard_allows_anyone():
    assert _access(dm_policy="allowlist", allow_from=["*"]).decide(_dm("7")) == "allow"


def test_allow_from_accepts_bare_snowflake():
    assert _access(dm_policy="allowlist", allow_from=["1"]).decide(_dm("1")) == "allow"


def test_dm_allowlist_accepts_human_user_name():
    access = _access(dm_policy="allowlist", allow_from=["ricardo"])

    assert access.decide(_dm("1", author_names=frozenset({"Ricardo", "rdecal"}))) == "allow"


# --- guild / group policy ----------------------------------------------------


def test_group_disabled_denies_guild_message():
    access = _access(group_policy="disabled")
    assert access.decide(_guild(mentions_bot=True)) == "deny"


def test_group_allowlist_denies_unconfigured_guild():
    access = _access(group_policy="allowlist")  # no guilds configured
    assert access.decide(_guild(mentions_bot=True)) == "deny"


def test_group_open_allows_mentioned_message_in_unconfigured_guild():
    access = _access(group_policy="open")
    assert access.decide(_guild(mentions_bot=True)) == "allow"


def test_group_open_denies_unmentioned_message_by_default():
    access = _access(group_policy="open")
    assert access.decide(_guild(mentions_bot=False)) == "deny"


# --- require_mention ---------------------------------------------------------


def test_configured_guild_requires_mention_by_default():
    access = _access(group_policy="allowlist", chat={"g1": {}})
    assert access.decide(_guild(mentions_bot=False)) == "deny"
    assert access.decide(_guild(mentions_bot=True)) == "allow"


def test_channel_require_mention_false_overrides_guild():
    access = _access(
        group_policy="allowlist",
        chat={"g1": {"require_mention": True, "channels": {"c1": {"require_mention": False}}}},
    )
    assert access.decide(_guild(channel_id="c1", mentions_bot=False)) == "allow"


def test_name_configured_guild_channel_allows_message():
    access = _access(
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

    assert (
        access.decide(
            _guild(
                guild_id="123",
                guild_name="Synapse",
                channel_id="456",
                channel_name="code-improver",
                mentions_bot=False,
            )
        )
        == "allow"
    )


# --- channel enable / allowlist ----------------------------------------------


def test_disabled_channel_denies():
    access = _access(
        group_policy="allowlist",
        chat={"g1": {"channels": {"c1": {"enabled": False}}}},
    )
    assert access.decide(_guild(channel_id="c1", mentions_bot=True)) == "deny"


def test_channel_not_in_configured_allowlist_denies():
    access = _access(
        group_policy="allowlist",
        chat={"g1": {"channels": {"c1": {}}}},
    )
    # message in c2, but only c1 is allowed
    assert access.decide(_guild(channel_id="c2", mentions_bot=True)) == "deny"


# --- threads inherit parent-channel config -----------------------------------


def test_thread_inherits_parent_channel_config():
    access = _access(
        group_policy="allowlist",
        chat={"g1": {"channels": {"c1": {"require_mention": False}}}},
    )
    # message is in thread t1 whose parent is the configured channel c1
    ctx = _guild(channel_id="t1", parent_channel_id="c1", mentions_bot=False)
    assert access.decide(ctx) == "allow"


# --- member / role allowlists ------------------------------------------------


def test_member_users_allowlist_permits_listed_sender():
    access = _access(
        group_policy="allowlist",
        chat={"g1": {"require_mention": False, "users": ["discord:u1"]}},
    )
    assert access.decide(_guild(author_id="u1")) == "allow"


def test_member_users_allowlist_permits_human_user_name():
    access = _access(
        group_policy="allowlist",
        chat={"g1": {"require_mention": False, "users": ["ricardo"]}},
    )

    assert access.decide(_guild(author_names=frozenset({"Ricardo"}))) == "allow"


def test_member_users_allowlist_denies_unlisted_sender():
    access = _access(
        group_policy="allowlist",
        chat={"g1": {"require_mention": False, "users": ["discord:u1"]}},
    )
    assert access.decide(_guild(author_id="u2")) == "deny"


def test_member_role_allowlist_permits_sender_with_matching_role():
    access = _access(
        group_policy="allowlist",
        chat={"g1": {"require_mention": False, "roles": ["role:r1"]}},
    )
    ctx = _guild(author_id="u9", author_role_ids=frozenset({"r1"}))
    assert access.decide(ctx) == "allow"
