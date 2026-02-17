"""pynchy WhatsApp channel plugin."""

from __future__ import annotations

from typing import Any

import pluggy

from .channel import WhatsAppChannel

hookimpl = pluggy.HookimplMarker("pynchy")


class WhatsAppPlugin:
    """Plugin implementing selected pynchy hooks."""

    @hookimpl
    def pynchy_create_channel(self, context: Any) -> Any | None:
        if context is None:
            return None
        on_message = context.on_message_callback
        on_chat_metadata = context.on_chat_metadata_callback
        workspaces = context.workspaces
        return WhatsAppChannel(
            on_message=on_message,
            on_chat_metadata=on_chat_metadata,
            workspaces=workspaces,
        )
