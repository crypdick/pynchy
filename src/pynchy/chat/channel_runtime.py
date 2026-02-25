"""Channel plugin runtime helpers.

Loads and validates host-side channel plugins and resolves the default channel.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pynchy.config import get_settings
from pynchy.logger import logger
from pynchy.types import Channel, NewMessage, WorkspaceProfile

if TYPE_CHECKING:
    import pluggy


@dataclass(frozen=True)
class ChannelPluginContext:
    """Context passed to channel plugins via the create hook."""

    on_message_callback: Callable[[str, NewMessage], None]
    on_chat_metadata_callback: Callable[[str, str, str | None], None]
    workspaces: Callable[[], dict[str, WorkspaceProfile]]
    send_message: Callable[[str, str], Any]
    on_reaction_callback: Callable[[str, str, str, str], None] | None = None
    on_ask_user_answer_callback: Callable[[str, dict], None] | None = None


def default_channel_name() -> str:
    """Return configured command-center channel, falling back to tui."""
    configured = get_settings().command_center.connection
    if configured:
        return configured.strip()
    return "tui"


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

    logger.warning(
        "No channel plugins discovered; continuing in TUI-only mode. "
        "Add [plugins.<name>] entries in config.toml to enable external channels."
    )
    return []


def resolve_default_channel(channels: list[Channel]) -> Channel | None:
    """Resolve default channel by name from the loaded set."""
    wanted = default_channel_name()
    if wanted.lower() == "tui" or not channels:
        return None

    for channel in channels:
        if getattr(channel, "name", None) == wanted:
            return channel

    available = sorted(getattr(ch, "name", "?") for ch in channels)
    raise RuntimeError(
        f"Configured default channel '{wanted}' was not found. Available channels: {available}"
    )
