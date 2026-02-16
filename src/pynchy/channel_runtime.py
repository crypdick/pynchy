"""Channel plugin runtime helpers.

Loads and validates host-side channel plugins and resolves the default channel.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pynchy.config import get_settings
from pynchy.logger import logger
from pynchy.types import Channel, NewMessage, RegisteredGroup

if TYPE_CHECKING:
    import pluggy


@dataclass(frozen=True)
class ChannelPluginContext:
    """Context passed to channel plugins via the create hook."""

    on_message_callback: Callable[[str, NewMessage], None]
    on_chat_metadata_callback: Callable[[str, str, str | None], None]
    registered_groups: Callable[[], dict[str, RegisteredGroup]]
    send_message: Callable[[str, str], Any]


def default_channel_name() -> str:
    """Return configured default channel, falling back to whatsapp."""
    configured = get_settings().channels.default
    if configured:
        return configured.strip()
    return "whatsapp"


def load_channels(pm: pluggy.PluginManager, context: ChannelPluginContext) -> list[Channel]:
    """Create channel instances from plugin hooks."""
    candidates = pm.hook.pynchy_create_channel(context=context)
    channels = [c for c in candidates if c is not None]
    channels.sort(key=lambda ch: getattr(ch, "name", ""))

    if channels:
        logger.info(
            "Loaded channel plugins",
            channels=[getattr(ch, "name", "?") for ch in channels],
        )
        return channels

    install_hint = (
        "uv pip install git+https://github.com/crypdick/pynchy-plugin-whatsapp.git && "
        "uv run pynchy auth"
    )
    msg = (
        "No channel plugins were discovered. Install the default WhatsApp plugin with:\n"
        f"  {install_hint}"
    )
    raise RuntimeError(msg)


def resolve_default_channel(channels: list[Channel]) -> Channel:
    """Resolve default channel by name from the loaded set."""
    wanted = default_channel_name()
    for channel in channels:
        if getattr(channel, "name", None) == wanted:
            return channel

    available = sorted(getattr(ch, "name", "?") for ch in channels)
    raise RuntimeError(
        f"Configured default channel '{wanted}' was not found. Available channels: {available}"
    )
