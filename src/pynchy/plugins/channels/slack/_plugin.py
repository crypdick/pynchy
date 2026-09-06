"""Slack channel plugin implementation."""

from __future__ import annotations

import os
from collections.abc import (
    Callable,
)
from typing import Any

import pluggy

from pynchy.channels import (
    SlackConnectionSettings,
)
from pynchy.logger import logger
from pynchy.plugins.api import (
    ChannelPluginContext,
    NewMessage,
)

from ._cache import TtlCache
from ._channel import SlackChannel

hookimpl = pluggy.HookimplMarker("pynchy")

__all__ = [
    "SlackChannel",
    "SlackChannelPlugin",
    "TtlCache",
]


def _channel_context(
    context: ChannelPluginContext,
) -> tuple[
    Callable[[str, NewMessage], None],
    Callable[[str, str, str | None], None],
    Callable[[str, str, str, str], None] | None,
    Callable[[str, dict[str, Any]], None] | None,
    Callable[[str, str, str, str], None] | None,
]:
    """Return the callbacks SlackChannel needs."""
    return (
        context.on_message_callback,
        context.on_chat_metadata_callback,
        context.on_reaction_callback,
        context.on_ask_user_answer_callback,
        context.on_approval_decision_callback,
    )


def _build_channel(  # noqa: PLR0913 - plugin factory mirrors Slack connection config.
    *,
    name: str,
    settings: SlackConnectionSettings,
    on_message: Callable[[str, NewMessage], None],
    on_metadata: Callable[[str, str, str | None], None],
    on_reaction: Callable[[str, str, str, str], None] | None,
    on_ask_user_answer: Callable[[str, dict[str, Any]], None] | None,
    on_approval_decision: Callable[[str, str, str, str], None] | None,
) -> SlackChannel | None:
    """Build one SlackChannel or log why that connection was skipped."""
    connection_name = name
    bot_env = settings.bot_token_env.strip()
    app_env = settings.app_token_env.strip()
    if not bot_env or not app_env:
        logger.warning(
            "Slack connection skipped — empty token env var name",
            connection=connection_name,
            bot_token_env=settings.bot_token_env,
            app_token_env=settings.app_token_env,
        )
        return None

    chat_names = list(settings.chat_names)
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
        assistant_name=settings.assistant_name,
        allow_create=settings.allow_create,
        on_message=on_message,
        on_chat_metadata=on_metadata,
        on_reaction=on_reaction,
        on_ask_user_answer=on_ask_user_answer,
        on_approval_decision=on_approval_decision,
    )


class SlackChannelPlugin:
    """Built-in plugin that activates when Slack tokens are configured."""

    @hookimpl
    def pynchy_create_channel(
        self, context: ChannelPluginContext | None
    ) -> list[SlackChannel] | None:
        if context is None:
            return None
        if not context.slack_connections:
            logger.debug("Slack channel skipped — no connections configured")
            return None

        callbacks = _channel_context(context)
        on_message, on_metadata, on_reaction, on_ask_user_answer, on_approval_decision = callbacks
        channels: list[SlackChannel] = []

        for name, settings in context.slack_connections.items():
            channel = _build_channel(
                name=name,
                settings=settings,
                on_message=on_message,
                on_metadata=on_metadata,
                on_reaction=on_reaction,
                on_ask_user_answer=on_ask_user_answer,
                on_approval_decision=on_approval_decision,
            )
            if channel is not None:
                channels.append(channel)

        return channels or None
