"""Discord ↔ pynchy JID conversion helpers.

A Discord jid is ``discord:<kind>:<snowflake>`` where *kind* is:

- ``direct`` — a DM, keyed on the **user** snowflake (so the DM allowlist
  doubles as the DM access check).
- ``channel`` — a guild text channel *or* a thread, keyed on the channel
  snowflake. Threads are ordinary channels with their own snowflake, so they
  need no distinct kind.
- ``group`` — a group DM, keyed on the group-channel snowflake.
"""

from __future__ import annotations

from dataclasses import dataclass

JID_PREFIX = "discord:"

DM_KIND = "direct"
CHANNEL_KIND = "channel"
GROUP_KIND = "group"

_KINDS = frozenset({DM_KIND, CHANNEL_KIND, GROUP_KIND})


@dataclass(frozen=True)
class DiscordJid:
    """Parsed Discord jid: a kind plus the snowflake it addresses."""

    kind: str
    snowflake: str


def dm_jid(user_id: int | str) -> str:
    """Build the jid for a DM, keyed on the user snowflake."""
    return f"{JID_PREFIX}{DM_KIND}:{user_id}"


def channel_jid(channel_id: int | str) -> str:
    """Build the jid for a guild channel or thread, keyed on its snowflake."""
    return f"{JID_PREFIX}{CHANNEL_KIND}:{channel_id}"


def group_jid(channel_id: int | str) -> str:
    """Build the jid for a group DM, keyed on the group-channel snowflake."""
    return f"{JID_PREFIX}{GROUP_KIND}:{channel_id}"


def is_discord_jid(jid: str) -> bool:
    """Return True iff ``jid`` is addressed to the Discord channel."""
    return jid.startswith(JID_PREFIX)


def parse_jid(jid: str) -> DiscordJid:
    """Parse a Discord jid into its kind and snowflake.

    Raises ``ValueError`` if the jid is not a Discord jid or names an
    unknown kind.
    """
    if not is_discord_jid(jid):
        raise ValueError(f"not a Discord jid: {jid!r}")
    kind, _, snowflake = jid.removeprefix(JID_PREFIX).partition(":")
    if kind not in _KINDS:
        raise ValueError(f"unknown Discord jid kind: {kind!r}")
    return DiscordJid(kind=kind, snowflake=snowflake)


def snowflake_of(jid: str) -> str:
    """Return the snowflake a Discord jid addresses, ignoring kind."""
    return parse_jid(jid).snowflake
