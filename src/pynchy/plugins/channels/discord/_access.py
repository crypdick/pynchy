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

from pynchy.config.models import (
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
    channel_id: str  # for a thread, the thread's own snowflake
    parent_channel_id: str | None  # for a thread, its parent channel; else None
    author_role_ids: frozenset[str]
    mentions_bot: bool


def _strip_user_prefix(entry: str) -> str:
    for prefix in ("discord:", "user:"):
        if entry.startswith(prefix):
            return entry[len(prefix) :]
    return entry


def _strip_role_prefix(entry: str) -> str:
    return entry.removeprefix("role:")


def _matches_user(allow: list[str], author_id: str) -> bool:
    return any(entry == "*" or _strip_user_prefix(entry) == author_id for entry in allow)


def _matches_role(roles: list[str], author_role_ids: frozenset[str]) -> bool:
    return any(_strip_role_prefix(role) in author_role_ids for role in roles)


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
        return "allow" if _matches_user(self._cfg.allow_from, ctx.author_id) else "deny"

    def _decide_guild(self, ctx: InboundContext) -> Decision:
        if self._cfg.group_policy == "disabled":
            return "deny"

        guild = self._cfg.chat.get(ctx.guild_id or "")
        if guild is None:
            return self._decide_unconfigured_guild(ctx)

        # Threads inherit their parent channel's config.
        channel_key = ctx.parent_channel_id or ctx.channel_id
        channel = guild.channels.get(channel_key)

        # If the guild pins a channel allowlist, a message elsewhere is denied.
        if guild.channels and channel is None:
            return "deny"
        if channel is not None and not channel.enabled:
            return "deny"
        return self._decide_member(ctx, guild, channel)

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
        # Member/role allowlist, channel-first then guild.
        users = channel.users if channel and channel.users else guild.users
        roles = channel.roles if channel and channel.roles else guild.roles
        if (users or roles) and not (
            _matches_user(users, ctx.author_id) or _matches_role(roles, ctx.author_role_ids)
        ):
            return "deny"

        require_mention = (
            channel.require_mention
            if channel is not None and channel.require_mention is not None
            else guild.require_mention
        )
        if require_mention and not ctx.mentions_bot:
            return "deny"
        return "allow"
