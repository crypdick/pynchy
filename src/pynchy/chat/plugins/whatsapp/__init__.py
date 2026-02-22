"""pynchy WhatsApp channel plugin."""

from __future__ import annotations

from typing import Any
from pathlib import Path

import pluggy

from pynchy.config import get_settings
from pynchy.logger import logger

from .channel import WhatsAppChannel

hookimpl = pluggy.HookimplMarker("pynchy")


class WhatsAppPlugin:
    """Plugin implementing selected pynchy hooks."""

    @hookimpl
    def pynchy_create_channel(self, context: Any) -> Any | None:
        s = get_settings()
        configs = s.connection.whatsapp
        if not configs:
            logger.debug("WhatsApp channel skipped — no connections configured")
            return None
        if context is None:
            return None
        on_message = context.on_message_callback
        on_chat_metadata = context.on_chat_metadata_callback
        workspaces = context.workspaces
        channels: list[WhatsAppChannel] = []
        seen_paths: dict[str, str] = {}
        for name, cfg in configs.items():
            connection_name = f"connection.whatsapp.{name}"
            if cfg.auth_db_path:
                auth_db_path = Path(cfg.auth_db_path)
                if not auth_db_path.is_absolute():
                    auth_db_path = (s.project_root / auth_db_path).resolve()
            else:
                auth_db_path = (s.data_dir / "neonize.db").resolve()
            key = str(auth_db_path)
            if key in seen_paths:
                raise ValueError(
                    "WhatsApp auth_db_path must be unique per connection: "
                    f"{seen_paths[key]} and {name} both use {key}"
                )
            seen_paths[key] = name
            channels.append(
                WhatsAppChannel(
                    connection_name=connection_name,
                    auth_db_path=str(auth_db_path),
                    on_message=on_message,
                    on_chat_metadata=on_chat_metadata,
                    workspaces=workspaces,
                )
            )
        return channels
