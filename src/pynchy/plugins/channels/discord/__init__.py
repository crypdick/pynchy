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


class DiscordChannelPlugin:
    """Built-in plugin that activates when Discord connections are configured."""

    @hookimpl
    def pynchy_create_channel(self, context: Any) -> list[DiscordChannel] | None:
        s = get_settings()
        configs = s.connection.discord
        if not configs:
            logger.debug("Discord channel skipped — no connections configured")
            return None

        if context is None:
            return None
        on_message = getattr(context, "on_message_callback", None)
        on_metadata = getattr(context, "on_chat_metadata_callback", None)
        if on_message is None or on_metadata is None:
            return None

        on_reaction = getattr(context, "on_reaction_callback", None)
        on_ask_user_answer = getattr(context, "on_ask_user_answer_callback", None)
        workspaces = getattr(context, "workspaces", None)

        channels: list[DiscordChannel] = []
        for name, cfg in configs.items():
            connection_name = f"connection.discord.{name}"
            token_env = (cfg.bot_token_env or "").strip()
            if not token_env:
                logger.warning(
                    "Discord connection skipped — empty bot_token_env",
                    connection=connection_name,
                )
                continue
            token = os.environ.get(token_env, "")
            if not token:
                logger.warning(
                    "Discord connection skipped — missing token",
                    connection=connection_name,
                    bot_token_env=token_env,
                )
                continue

            channels.append(
                DiscordChannel(
                    connection_name=connection_name,
                    config=cfg,
                    bot_token=token,
                    on_message=on_message,
                    on_chat_metadata=on_metadata,
                    on_reaction=on_reaction,
                    on_ask_user_answer=on_ask_user_answer,
                    workspaces=workspaces,
                )
            )

        return channels or None
