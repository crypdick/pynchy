"""Discord channel plugin implementation."""

from __future__ import annotations

import os
from collections.abc import (
    Awaitable,
    Callable,
)
from pathlib import (
    Path,
)

import pluggy

from pynchy.discord import (
    DiscordConnectionSettings,
)
from pynchy.logger import logger
from pynchy.plugins.api import (
    AudioTranscriptionResult,
    ChannelPluginContext,
    InboundAudioProcessingRequest,
    InboundAudioProcessingResult,
    NewMessage,
)
from pynchy.plugins.speech.api import (
    SpeechSynthesizer,
)
from pynchy.workspace.api import (
    WorkspaceProfile,
)

from ._channel import DiscordChannel

hookimpl = pluggy.HookimplMarker("pynchy")

__all__ = ["DiscordChannel", "DiscordChannelPlugin"]


def _channel_context(
    context: ChannelPluginContext,
) -> tuple[
    Callable[[str, NewMessage], None],
    Callable[[str, str, str | None], None],
    Callable[[str, str, str, str], None] | None,
    Callable[[str, dict[str, object]], None] | None,
    Callable[[str, str, str, str], None] | None,
    Callable[[], dict[str, WorkspaceProfile]] | None,
    SpeechSynthesizer | None,
    Callable[[Path], Awaitable[AudioTranscriptionResult]] | None,
    Callable[[InboundAudioProcessingRequest], Awaitable[InboundAudioProcessingResult]] | None,
    Callable[[str], Awaitable[list[str]]] | None,
]:
    """Return the callbacks DiscordChannel needs."""
    return (
        context.on_message_callback,
        context.on_chat_metadata_callback,
        context.on_reaction_callback,
        context.on_ask_user_answer_callback,
        context.on_approval_decision_callback,
        context.workspaces,
        context.speech_synthesizer,
        context.transcribe_audio,
        context.process_inbound_audio,
        context.find_chat_jids_by_name,
    )


def _build_channel(  # noqa: PLR0913 - plugin factory keeps channel wiring explicit.
    *,
    name: str,
    cfg: DiscordConnectionSettings,
    on_message: Callable[[str, NewMessage], None],
    on_metadata: Callable[[str, str, str | None], None],
    on_reaction: Callable[[str, str, str, str], None] | None,
    on_ask_user_answer: Callable[[str, dict[str, object]], None] | None,
    on_approval_decision: Callable[[str, str, str, str], None] | None,
    workspaces: Callable[[], dict[str, WorkspaceProfile]] | None,
    speech_synthesizer: SpeechSynthesizer | None,
    transcribe_audio: Callable[[Path], Awaitable[AudioTranscriptionResult]] | None,
    process_inbound_audio: (
        Callable[[InboundAudioProcessingRequest], Awaitable[InboundAudioProcessingResult]] | None
    ),
    find_chat_jids_by_name: Callable[[str], Awaitable[list[str]]] | None,
    audio_cache_dir: Path,
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
        transcribe_audio=transcribe_audio,
        process_inbound_audio=process_inbound_audio,
        find_chat_jids_by_name=find_chat_jids_by_name,
        audio_cache_dir=audio_cache_dir,
    )


class DiscordChannelPlugin:
    """Built-in plugin that activates when Discord connections are configured."""

    @hookimpl
    def pynchy_create_channel(
        self, context: ChannelPluginContext | None
    ) -> list[DiscordChannel] | None:
        if context is None:
            return None
        configs = context.discord_connections
        if not configs:
            logger.debug("Discord channel skipped — no connections configured")
            return None

        callbacks = _channel_context(context)
        (
            on_message,
            on_metadata,
            on_reaction,
            on_ask_user_answer,
            on_approval_decision,
            workspaces,
            speech_synthesizer,
            transcribe_audio,
            process_inbound_audio,
            find_chat_jids_by_name,
        ) = callbacks

        channels: list[DiscordChannel] = []
        audio_cache_dir = context.discord_audio_cache_dir
        if audio_cache_dir is None:
            raise RuntimeError("Discord channel requires an audio cache directory")
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
                transcribe_audio=transcribe_audio,
                process_inbound_audio=process_inbound_audio,
                find_chat_jids_by_name=find_chat_jids_by_name,
                audio_cache_dir=audio_cache_dir,
            )
            if channel is not None:
                channels.append(channel)

        return channels or None
