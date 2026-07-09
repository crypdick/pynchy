"""Slack channel plugin implementation."""

from __future__ import annotations

import os
import sys
from typing import Any

import pluggy

from pynchy.logger import logger

from ._cache import TtlCache
from ._channel import SlackChannel, _channel_id_from_jid, _jid
from ._ui import normalize_chat_name as _normalize_chat_name
from ._ui import split_text as _split_text

hookimpl = pluggy.HookimplMarker("pynchy")

__all__ = [
    "SlackChannel",
    "SlackChannelPlugin",
    "TtlCache",
    "_channel_id_from_jid",
    "_jid",
    "_normalize_chat_name",
    "_split_text",
]


def _public_module() -> Any:
    return sys.modules[__package__]


def _channel_context(context: Any) -> tuple[Any, Any, Any, Any] | None:
    """Return the callbacks SlackChannel needs, or ``None`` when unavailable."""
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
    )


def _build_channel(
    *,
    name: str,
    cfg: Any,
    settings: Any,
    on_message: Any,
    on_metadata: Any,
    on_reaction: Any,
    on_ask_user_answer: Any,
) -> SlackChannel | None:
    """Build one SlackChannel or log why that connection was skipped."""
    connection_name = name
    bot_env = (cfg.bot_token_env or "").strip()
    app_env = (cfg.app_token_env or "").strip()
    if not bot_env or not app_env:
        logger.warning(
            "Slack connection skipped — empty token env var name",
            connection=connection_name,
            bot_token_env=cfg.bot_token_env,
            app_token_env=cfg.app_token_env,
        )
        return None

    chat_names = list(cfg.chat.keys())
    if not chat_names:
        logger.warning(
            "Slack connection has no configured chats; skipping",
            connection=connection_name,
        )
        return None

    bot_token = os.environ.get(bot_env, "")
    app_token = os.environ.get(app_env, "")
    if not bot_token or not app_token:
        logger.warning(
            "Slack connection skipped — missing tokens",
            connection=connection_name,
            bot_token_env=bot_env,
            app_token_env=app_env,
        )
        return None

    return SlackChannel(
        connection_name=connection_name,
        bot_token=bot_token,
        app_token=app_token,
        chat_names=chat_names,
        allow_create=settings.command_center.connection == connection_name,
        on_message=on_message,
        on_chat_metadata=on_metadata,
        on_reaction=on_reaction,
        on_ask_user_answer=on_ask_user_answer,
    )


class SlackChannelPlugin:
    """Built-in plugin that activates when Slack tokens are configured."""

    @hookimpl
    def pynchy_create_channel(self, context: Any) -> list[SlackChannel] | None:
        settings = _public_module().get_settings()
        configs = {name: cfg for name, cfg in settings.connections.items() if cfg.type == "slack"}
        if not configs:
            logger.debug("Slack channel skipped — no connections configured")
            return None

        callbacks = _channel_context(context)
        if callbacks is None:
            return None
        on_message, on_metadata, on_reaction, on_ask_user_answer = callbacks
        channels: list[SlackChannel] = []

        for name, cfg in configs.items():
            channel = _build_channel(
                name=name,
                cfg=cfg,
                settings=settings,
                on_message=on_message,
                on_metadata=on_metadata,
                on_reaction=on_reaction,
                on_ask_user_answer=on_ask_user_answer,
            )
            if channel is not None:
                channels.append(channel)

        return channels or None
