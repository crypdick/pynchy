"""Inbound access-control for the Discord channel.

``DiscordAccess.decide`` is a pure decision over an :class:`InboundContext`
(primitives already pulled off a ``discord.Message``) plus the connection
config. Keeping it free of ``discord`` types makes the whole allow/deny/pairing
tree exhaustively unit-testable without a live gateway.

Two Discord-specific rules the Slack channel doesn't have:

- **Threads inherit their parent channel's config.** A thread has its own
  snowflake that won't appear in config, so the parent channel id is used for
  the channel-config lookup.
- **Channel settings fall back to the guild.** ``require_mention`` and the
  member/role allowlists resolve channel-first, then guild.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pynchy.config.models import (  # noqa: TC001, RUF100 - beartype resolves these runtime annotations.
    DiscordChannelConfig,
    DiscordConnectionConfig,
    DiscordGuildConfig,
)

Decision = Literal["allow", "deny", "pairing"]


@dataclass(frozen=True)
class InboundContext:
    """Primitives extracted from an inbound Discord message for access checks."""

    is_dm: bool
    author_id: str
    author_is_bot: bool
    guild_id: str | None
    guild_name: str | None
    channel_id: str  # for a thread, the thread's own snowflake
    channel_name: str | None
    parent_channel_id: str | None  # for a thread, its parent channel; else None
    parent_channel_name: str | None
    author_role_ids: frozenset[str]
    mentions_bot: bool
    author_names: frozenset[str] = frozenset()


def _strip_user_prefix(entry: str) -> str:
    for prefix in ("discord:", "user:"):
        if entry.startswith(prefix):
            return entry[len(prefix) :]
    return entry


def _strip_role_prefix(entry: str) -> str:
    return entry.removeprefix("role:")


def _same_ref(left: str, right: str) -> bool:
    return left.casefold() == right.casefold()


def _matches_user(allow: list[str], author_id: str, author_names: frozenset[str]) -> bool:
    for entry in allow:
        stripped = _strip_user_prefix(entry)
        if entry == "*" or stripped == author_id:
            return True
        if any(_same_ref(stripped, name) for name in author_names):
            return True
    return False


def _matches_role(roles: list[str], author_role_ids: frozenset[str]) -> bool:
    return any(_strip_role_prefix(role) in author_role_ids for role in roles)


def _same_name(left: str | None, right: str | None) -> bool:
    return bool(left and right and left.casefold() == right.casefold())


def _member_allowlists(
    guild: DiscordGuildConfig, channel: DiscordChannelConfig | None
) -> tuple[list[str], list[str]]:
    users = channel.users if channel and channel.users else guild.users
    roles = channel.roles if channel and channel.roles else guild.roles
    return users, roles


def _is_member_allowed(
    ctx: InboundContext,
    *,
    users: list[str],
    roles: list[str],
) -> bool:
    if not users and not roles:
        return True
    return _matches_user(users, ctx.author_id, ctx.author_names) or _matches_role(
        roles, ctx.author_role_ids
    )


def _requires_mention(
    guild: DiscordGuildConfig, channel: DiscordChannelConfig | None
) -> bool | None:
    if channel is not None and channel.require_mention is not None:
        return channel.require_mention
    return guild.require_mention


class DiscordAccess:
    """Decide whether an inbound message may reach the agent."""

    def __init__(self, config: DiscordConnectionConfig) -> None:
        self._cfg = config

    def decide(self, ctx: InboundContext) -> Decision:
        if ctx.author_is_bot:
            return "deny"  # allow_bots default off
        if ctx.is_dm:
            return self._decide_dm(ctx)
        return self._decide_guild(ctx)

    def _decide_dm(self, ctx: InboundContext) -> Decision:
        policy = self._cfg.dm_policy
        if policy == "disabled":
            return "deny"
        if policy == "open":
            return "allow"
        # allowlist: matched senders pass; unmatched are denied in v1 (a future
        # pairing collaborator would return "pairing" here instead).
        return (
            "allow"
            if _matches_user(self._cfg.allow_from, ctx.author_id, ctx.author_names)
            else "deny"
        )

    def _decide_guild(self, ctx: InboundContext) -> Decision:
        if self._cfg.group_policy == "disabled":
            return "deny"

        guild = self._lookup_guild(ctx)
        if guild is None:
            return self._decide_unconfigured_guild(ctx)

        # Threads inherit their parent channel's config.
        channel = self._lookup_channel(guild, ctx)

        # If the guild pins a channel allowlist, a message elsewhere is denied.
        if guild.channels and channel is None:
            return "deny"
        if channel is not None and not channel.enabled:
            return "deny"
        return self._decide_member(ctx, guild, channel)

    def _lookup_guild(self, ctx: InboundContext) -> DiscordGuildConfig | None:
        for key in (ctx.guild_id, ctx.guild_name):
            if key and key in self._cfg.chat:
                return self._cfg.chat[key]
        return next(
            (guild for guild in self._cfg.chat.values() if _same_name(guild.name, ctx.guild_name)),
            None,
        )

    def _lookup_channel(
        self, guild: DiscordGuildConfig, ctx: InboundContext
    ) -> DiscordChannelConfig | None:
        for key in (
            ctx.parent_channel_id,
            ctx.channel_id,
            ctx.parent_channel_name,
            ctx.channel_name,
        ):
            if key and key in guild.channels:
                return guild.channels[key]
        lookup_name = ctx.parent_channel_name or ctx.channel_name
        return next(
            (
                channel
                for channel in guild.channels.values()
                if _same_name(channel.name, lookup_name)
            ),
            None,
        )

    def _decide_unconfigured_guild(self, ctx: InboundContext) -> Decision:
        # Open policy still permits mention-gated messages; allowlist rejects.
        if self._cfg.group_policy == "open":
            return "allow" if ctx.mentions_bot else "deny"
        return "deny"

    def _decide_member(
        self,
        ctx: InboundContext,
        guild: DiscordGuildConfig,
        channel: DiscordChannelConfig | None,
    ) -> Decision:
        users, roles = _member_allowlists(guild, channel)
        if not _is_member_allowed(ctx, users=users, roles=roles):
            return "deny"
        require_mention = _requires_mention(guild, channel)
        if require_mention and not ctx.mentions_bot:
            return "deny"
        return "allow"
