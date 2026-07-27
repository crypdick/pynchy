"""Slack channel plugin implementation."""

from __future__ import annotations

import os
import sys
from collections.abc import (
    Callable,  # noqa: TC003, RUF100 - beartype resolves this runtime annotation.
)
from typing import Any, Protocol, cast, runtime_checkable

import pluggy

from pynchy.config.models import (  # noqa: TC001, RUF100 - beartype resolves plugin config annotations at runtime.
    SlackConnectionConfig,
)
from pynchy.config.settings import (
    Settings,  # noqa: TC001, RUF100 - beartype resolves this runtime annotation.
)
from pynchy.logger import logger
from pynchy.plugins.channel_runtime import (  # noqa: TC001, RUF100 - beartype resolves hook annotations at runtime.
    ChannelPluginContext,
)
from pynchy.types import (
    NewMessage,  # noqa: TC001, RUF100 - beartype resolves this runtime annotation.
)

from ._cache import TtlCache
from ._channel import SlackChannel

hookimpl = pluggy.HookimplMarker("pynchy")

__all__ = [
    "SlackChannel",
    "SlackChannelPlugin",
    "TtlCache",
]


@runtime_checkable
class _SlackPublicModule(Protocol):
    def get_settings(self) -> Settings: ...


def _public_module() -> _SlackPublicModule:
    return cast("_SlackPublicModule", sys.modules[__package__])


def _channel_context(
    context: ChannelPluginContext | None,
) -> (
    tuple[
        Callable[[str, NewMessage], None],
        Callable[[str, str, str | None], None],
        Callable[[str, str, str, str], None] | None,
        Callable[[str, dict[str, Any]], None] | None,
        Callable[[str, str, str, str], None] | None,
    ]
    | None
):
    """Return the callbacks SlackChannel needs, or ``None`` when unavailable."""
    if context is None:
        return None
    return (
        context.on_message_callback,
        context.on_chat_metadata_callback,
        context.on_reaction_callback,
        context.on_ask_user_answer_callback,
        context.on_approval_decision_callback,
    )


def _build_channel(  # noqa: PLR0913, RUF100 - plugin factory mirrors Slack connection config.
    *,
    name: str,
    cfg: SlackConnectionConfig,
    settings: Settings,
    on_message: Callable[[str, NewMessage], None],
    on_metadata: Callable[[str, str, str | None], None],
    on_reaction: Callable[[str, str, str, str], None] | None,
    on_ask_user_answer: Callable[[str, dict[str, Any]], None] | None,
    on_approval_decision: Callable[[str, str, str, str], None] | None,
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
        assistant_name=settings.agent.name,
        allow_create=settings.command_center.connection == connection_name,
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
        settings = _public_module().get_settings()
        configs = {name: cfg for name, cfg in settings.connections.items() if cfg.type == "slack"}
        if not configs:
            logger.debug("Slack channel skipped — no connections configured")
            return None

        callbacks = _channel_context(context)
        if callbacks is None:
            return None
        on_message, on_metadata, on_reaction, on_ask_user_answer, on_approval_decision = callbacks
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
                on_approval_decision=on_approval_decision,
            )
            if channel is not None:
                channels.append(channel)

        return channels or None
