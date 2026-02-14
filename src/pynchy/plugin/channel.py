"""Channel plugin system for communication platforms.

Enables new messaging platforms (Telegram, Slack, Discord, etc.) to be
added as plugins while WhatsApp remains built-in.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pynchy.plugin.base import PluginBase

if TYPE_CHECKING:
    from pynchy.types import Channel, RegisteredGroup


@dataclass
class PluginContext:
    """Context object passed to plugins during initialization.

    Provides access to host services that plugins may need.
    """

    registered_groups: Callable[[], dict[str, RegisteredGroup]]
    """Callable that returns the current registered groups dict."""

    send_message: Callable[[str, str], Awaitable[None]]
    """Async function to send a message to a JID."""

    # Add more services as needed by plugins


class ChannelPlugin(PluginBase):
    """Base class for channel plugins.

    Channel plugins provide new communication platforms. They create a Channel
    instance that integrates with pynchy's multi-channel message routing.

    WhatsApp remains built-in; this is for additional platforms.

    .. warning:: **Unsandboxed — highest risk plugin category.**

       Channel plugins run **persistently in the host process** (not in a
       container). The ``create_channel()`` return value lives for the entire
       application lifetime and has full access to the host filesystem,
       network, and Python runtime. A malicious or buggy channel plugin can
       read secrets, exfiltrate data, or crash the host process.

       **Only install channel plugins from authors you trust.**
    """

    categories = ["channel"]  # Fixed category for all channel plugins

    @abstractmethod
    def create_channel(self, ctx: PluginContext) -> Channel:
        """Create and return a Channel instance.

        Called during app startup. The returned channel will be connected
        alongside the built-in WhatsApp channel.

        Args:
            ctx: Context object providing access to host services

        Returns:
            Channel: An object implementing the Channel protocol from types.py
        """
        ...

    def requires_credentials(self) -> list[str]:
        """Return list of required environment variable names.

        Optional hook for validation. If implemented, these env vars are
        checked during startup. Missing credentials cause a warning.

        Example:
            return ["TELEGRAM_BOT_TOKEN", "TELEGRAM_API_ID"]

        Returns:
            List of environment variable names
        """
        return []
