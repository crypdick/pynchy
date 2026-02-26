"""Built-in Slack channel plugin.

Connects to Slack via Socket Mode (bolt) and maps Slack channels/DMs to
pynchy workspaces.  Each Slack conversation is identified by a JID of the
form ``slack:<CHANNEL_ID>`` so it coexists with other channel plugins.

Activation: define ``[connection.slack.<name>]`` entries in config.toml and
provide token env var names (e.g. ``SLACK__BOT_TOKEN`` / ``SLACK__APP_TOKEN``).
The plugin returns ``None`` when no Slack connections are configured, so it
never interferes with installations that don't use Slack.

Package layout:
  _channel.py — SlackChannel class (core protocol implementation)
  _ui.py      — Block Kit builders and text utilities
"""

from __future__ import annotations

import os
from typing import Any

import pluggy

from pynchy.config import get_settings
from pynchy.logger import logger

from ._channel import SlackChannel, _channel_id_from_jid, _jid, _TtlCache
from ._ui import normalize_chat_name as _normalize_chat_name
from ._ui import split_text as _split_text

hookimpl = pluggy.HookimplMarker("pynchy")

# Re-export under original names for backwards compatibility (tests import these).
__all__ = [
    "SlackChannel",
    "SlackChannelPlugin",
    "_TtlCache",
    "_channel_id_from_jid",
    "_jid",
    "_normalize_chat_name",
    "_split_text",
]


class SlackChannelPlugin:
    """Built-in plugin that activates when Slack tokens are configured."""

    @hookimpl
    def pynchy_create_channel(self, context: Any) -> list[SlackChannel] | None:
        s = get_settings()
        configs = s.connection.slack
        if not configs:
            logger.debug("Slack channel skipped — no connections configured")
            return None

        # Guard against None/incomplete context (e.g. in tests)
        if context is None:
            return None
        on_message = getattr(context, "on_message_callback", None)
        on_metadata = getattr(context, "on_chat_metadata_callback", None)
        if on_message is None or on_metadata is None:
            return None

        on_reaction = getattr(context, "on_reaction_callback", None)
        on_ask_user_answer = getattr(context, "on_ask_user_answer_callback", None)
        channels: list[SlackChannel] = []

        for name, cfg in configs.items():
            connection_name = f"connection.slack.{name}"
            bot_env = (cfg.bot_token_env or "").strip()
            app_env = (cfg.app_token_env or "").strip()
            if not bot_env or not app_env:
                logger.warning(
                    "Slack connection skipped — empty token env var name",
                    connection=connection_name,
                    bot_token_env=cfg.bot_token_env,
                    app_token_env=cfg.app_token_env,
                )
                continue
            bot_token = os.environ.get(bot_env, "")
            app_token = os.environ.get(app_env, "")
            chat_names = list(cfg.chat.keys())

            if not chat_names:
                logger.warning(
                    "Slack connection has no configured chats; skipping",
                    connection=connection_name,
                )
                continue

            if not bot_token or not app_token:
                logger.warning(
                    "Slack connection skipped — missing tokens",
                    connection=connection_name,
                    bot_token_env=bot_env,
                    app_token_env=app_env,
                )
                continue

            allow_create = s.command_center.connection == connection_name

            channels.append(
                SlackChannel(
                    connection_name=connection_name,
                    bot_token=bot_token,
                    app_token=app_token,
                    chat_names=chat_names,
                    allow_create=allow_create,
                    on_message=on_message,
                    on_chat_metadata=on_metadata,
                    on_reaction=on_reaction,
                    on_ask_user_answer=on_ask_user_answer,
                )
            )

        return channels or None
