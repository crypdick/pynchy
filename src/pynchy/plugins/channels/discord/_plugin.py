"""Discord channel plugin implementation."""

from __future__ import annotations

import os
import sys
from collections.abc import (
    Callable,  # noqa: TC003, RUF100 - beartype resolves this runtime annotation.
)
from typing import cast

import pluggy

from pynchy.config.models import (  # noqa: TC001, RUF100 - beartype resolves plugin config annotations at runtime.
    DiscordConnectionConfig,
)
from pynchy.config.settings import (
    Settings,  # noqa: TC001, RUF100 - beartype resolves this runtime annotation.
)
from pynchy.logger import logger
from pynchy.plugins.channel_runtime import (  # noqa: TC001, RUF100 - beartype resolves hook annotations at runtime.
    ChannelPluginContext,
)
from pynchy.plugins.speech import (  # noqa: TC001, RUF100 - beartype resolves plugin annotations at runtime.
    SpeechSynthesizer,
)
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves this runtime annotation.
    NewMessage,
    WorkspaceProfile,
)

from ._channel import DiscordChannel

hookimpl = pluggy.HookimplMarker("pynchy")

__all__ = ["DiscordChannel", "DiscordChannelPlugin"]


def _public_module() -> object:
    return sys.modules[__package__]


def _channel_context(
    context: ChannelPluginContext | None,
) -> (
    tuple[
        Callable[[str, NewMessage], None],
        Callable[[str, str, str | None], None],
        Callable[[str, str, str, str], None] | None,
        Callable[[str, dict[str, object]], None] | None,
        Callable[[str, str, str, str], None] | None,
        Callable[[], dict[str, WorkspaceProfile]] | None,
        SpeechSynthesizer | None,
    ]
    | None
):
    """Return the callbacks DiscordChannel needs, or ``None`` when unavailable."""
    if context is None:
        return None
    return (
        context.on_message_callback,
        context.on_chat_metadata_callback,
        context.on_reaction_callback,
        context.on_ask_user_answer_callback,
        context.on_approval_decision_callback,
        context.workspaces,
        context.speech_synthesizer,
    )


def _build_channel(  # noqa: PLR0913, RUF100 - plugin factory keeps channel wiring explicit.
    *,
    name: str,
    cfg: DiscordConnectionConfig,
    on_message: Callable[[str, NewMessage], None],
    on_metadata: Callable[[str, str, str | None], None],
    on_reaction: Callable[[str, str, str, str], None] | None,
    on_ask_user_answer: Callable[[str, dict[str, object]], None] | None,
    on_approval_decision: Callable[[str, str, str, str], None] | None,
    workspaces: Callable[[], dict[str, WorkspaceProfile]] | None,
    speech_synthesizer: SpeechSynthesizer | None,
) -> DiscordChannel | None:
    """Build one DiscordChannel or log why that connection was skipped."""
    connection_name = name
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
        on_approval_decision=on_approval_decision,
        workspaces=workspaces,
        speech_synthesizer=speech_synthesizer,
    )


class DiscordChannelPlugin:
    """Built-in plugin that activates when Discord connections are configured."""

    @hookimpl
    def pynchy_create_channel(
        self, context: ChannelPluginContext | None
    ) -> list[DiscordChannel] | None:
        public = _public_module()
        get_settings = cast("Callable[[], Settings]", public.get_settings)
        settings = get_settings()
        configs = {name: cfg for name, cfg in settings.connections.items() if cfg.type == "discord"}
        if not configs:
            logger.debug("Discord channel skipped — no connections configured")
            return None

        callbacks = _channel_context(context)
        if callbacks is None:
            return None
        (
            on_message,
            on_metadata,
            on_reaction,
            on_ask_user_answer,
            on_approval_decision,
            workspaces,
            speech_synthesizer,
        ) = callbacks

        channels: list[DiscordChannel] = []
        for name, cfg in configs.items():
            channel = _build_channel(
                name=name,
                cfg=cfg,
                on_message=on_message,
                on_metadata=on_metadata,
                on_reaction=on_reaction,
                on_ask_user_answer=on_ask_user_answer,
                on_approval_decision=on_approval_decision,
                workspaces=workspaces,
                speech_synthesizer=speech_synthesizer,
            )
            if channel is not None:
                channels.append(channel)

        return channels or None
