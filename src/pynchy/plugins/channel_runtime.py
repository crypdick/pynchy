"""Channel plugin runtime helpers.

Loads and validates host-side channel plugins and resolves the default channel.
"""

from __future__ import annotations

from collections.abc import (
    Awaitable,
    Callable,
)
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pluggy

from pynchy.channels import (
    SlackConnectionSettings,
    WhatsAppConnectionSettings,
)
from pynchy.discord import (
    DiscordConnectionSettings,
)
from pynchy.logger import logger
from pynchy.plugins.contracts import (
    AudioTranscriptionResult,
    Channel,
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

_DEFAULT_CHANNEL_NOT_FOUND = (
    "Configured default channel '{wanted}' was not found. Available channels: {available}"
)


@dataclass(frozen=True)
class ChannelPluginContext:
    """Context passed to channel plugins via the create hook."""

    on_message_callback: Callable[[str, NewMessage], None]
    on_chat_metadata_callback: Callable[[str, str, str | None], None]
    workspaces: Callable[[], dict[str, WorkspaceProfile]]
    send_message: Callable[[str, str], Any]
    on_reaction_callback: Callable[[str, str, str, str], None] | None = None
    on_ask_user_answer_callback: Callable[[str, dict[str, Any]], None] | None = None
    on_approval_decision_callback: Callable[[str, str, str, str], None] | None = None
    speech_synthesizer: SpeechSynthesizer | None = None
    transcribe_audio: Callable[[Path], Awaitable[AudioTranscriptionResult]] | None = None
    process_inbound_audio: (
        Callable[[InboundAudioProcessingRequest], Awaitable[InboundAudioProcessingResult]] | None
    ) = None
    find_chat_jids_by_name: Callable[[str], Awaitable[list[str]]] | None = None
    get_last_group_sync: Callable[[], Awaitable[str | None]] | None = None
    set_last_group_sync: Callable[[], Awaitable[None]] | None = None
    update_chat_name: Callable[[str, str], Awaitable[None]] | None = None
    discord_connections: dict[str, DiscordConnectionSettings] = field(default_factory=dict)
    discord_audio_cache_dir: Path | None = None
    slack_connections: dict[str, SlackConnectionSettings] = field(default_factory=dict)
    whatsapp_connections: dict[str, WhatsAppConnectionSettings] = field(default_factory=dict)


def default_channel_name(configured: str | None) -> str | None:
    """Normalize the explicitly configured command-center channel."""
    if configured:
        return configured.strip()
    return None


def load_channels(pm: pluggy.PluginManager, context: ChannelPluginContext) -> list[Channel]:
    """Create channel instances from plugin hooks."""
    candidates = pm.hook.pynchy_create_channel(context=context)
    channels: list[Channel] = []
    for c in candidates:
        if c is None:
            continue
        if isinstance(c, list | tuple):
            channels.extend([item for item in c if item is not None])
        else:
            channels.append(c)
    channels.sort(key=lambda ch: getattr(ch, "name", ""))

    if channels:
        logger.info(
            "Loaded channel plugins",
            channels=[getattr(ch, "name", "?") for ch in channels],
        )
        return channels

    logger.warning("No channel plugins discovered")
    return []


def resolve_default_channel(channels: list[Channel], configured_name: str | None) -> Channel | None:
    """Resolve default channel by name from the loaded set."""
    wanted = default_channel_name(configured_name)
    if wanted is None or not channels:
        return None

    for channel in channels:
        if getattr(channel, "name", None) == wanted:
            return channel

    available = sorted(getattr(ch, "name", "?") for ch in channels)
    raise RuntimeError(_DEFAULT_CHANNEL_NOT_FOUND.format(wanted=wanted, available=available))
