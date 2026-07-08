"""Built-in Discord channel plugin.

Connects to Discord as a bot over the official gateway (via discord.py) and
maps guild channels, threads, and DMs to pynchy conversations. Each Discord
conversation is a jid of the form ``discord:<kind>:<snowflake>`` (see
``_ids``), so it coexists with other channel plugins.

Activation: define ``[connection.discord.<name>]`` entries in config.toml with
a ``bot_token_env`` naming the env var that holds the bot token. The plugin
returns ``None`` when no Discord connections are configured, so it never
interferes with installations that don't use Discord.

Package layout:
  _channel.py   — DiscordChannel (composition root + outbound protocol)
  _lifecycle.py — connect/disconnect/reconnect over discord.Client
  _events.py    — inbound message/reaction handling
  _access.py    — allow/deny decision tree
  _chunk.py     — 2000-char fence-aware splitter
  _ids.py       — jid helpers
"""

from __future__ import annotations

import os
from typing import Any

import pluggy

from pynchy.config import get_settings
from pynchy.logger import logger

from ._channel import DiscordChannel

hookimpl = pluggy.HookimplMarker("pynchy")

__all__ = ["DiscordChannel", "DiscordChannelPlugin"]


def _channel_context(context: Any) -> tuple[Any, Any, Any, Any, Any] | None:
    """Return the callbacks DiscordChannel needs, or ``None`` when unavailable."""
    if context is None:
        return None
    on_message = getattr(context, "on_message_callback", None)
    on_metadata = getattr(context, "on_chat_metadata_callback", None)
    if on_message is None or on_metadata is None:
        return None
    return (
        on_message,
        on_metadata,
        getattr(context, "on_reaction_callback", None),
        getattr(context, "on_ask_user_answer_callback", None),
        getattr(context, "workspaces", None),
    )


def _build_channel(
    *,
    name: str,
    cfg: Any,
    on_message: Any,
    on_metadata: Any,
    on_reaction: Any,
    on_ask_user_answer: Any,
    workspaces: Any,
) -> DiscordChannel | None:
    """Build one DiscordChannel or log why that connection was skipped."""
    connection_name = f"connection.discord.{name}"
    token_env = (cfg.bot_token_env or "").strip()
    if not token_env:
        logger.warning(
            "Discord connection skipped — empty bot_token_env",
            connection=connection_name,
        )
        return None

    token = os.environ.get(token_env, "")
    if not token:
        logger.warning(
            "Discord connection skipped — missing token",
            connection=connection_name,
            bot_token_env=token_env,
        )
        return None

    return DiscordChannel(
        connection_name=connection_name,
        config=cfg,
        bot_token=token,
        on_message=on_message,
        on_chat_metadata=on_metadata,
        on_reaction=on_reaction,
        on_ask_user_answer=on_ask_user_answer,
        workspaces=workspaces,
    )


class DiscordChannelPlugin:
    """Built-in plugin that activates when Discord connections are configured."""

    @hookimpl
    def pynchy_create_channel(self, context: Any) -> list[DiscordChannel] | None:
        settings = get_settings()
        configs = settings.connection.discord
        if not configs:
            logger.debug("Discord channel skipped — no connections configured")
            return None

        callbacks = _channel_context(context)
        if callbacks is None:
            return None
        on_message, on_metadata, on_reaction, on_ask_user_answer, workspaces = callbacks

        channels: list[DiscordChannel] = []
        for name, cfg in configs.items():
            channel = _build_channel(
                name=name,
                cfg=cfg,
                on_message=on_message,
                on_metadata=on_metadata,
                on_reaction=on_reaction,
                on_ask_user_answer=on_ask_user_answer,
                workspaces=workspaces,
            )
            if channel is not None:
                channels.append(channel)

        return channels or None
