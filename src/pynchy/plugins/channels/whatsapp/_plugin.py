"""WhatsApp channel plugin implementation."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pluggy

from pynchy.logger import logger

hookimpl = pluggy.HookimplMarker("pynchy")


def _public_module() -> Any:
    return sys.modules[__package__]


class WhatsAppPlugin:
    """Plugin implementing selected pynchy hooks."""

    @hookimpl
    def pynchy_create_channel(self, context: Any) -> Any | None:
        public = _public_module()
        s = public.get_settings()
        configs = {name: cfg for name, cfg in s.connections.items() if cfg.type == "whatsapp"}
        if not configs:
            logger.debug("WhatsApp channel skipped — no connections configured")
            return None
        if context is None:
            return None
        on_message = context.on_message_callback
        on_chat_metadata = context.on_chat_metadata_callback
        workspaces = context.workspaces
        on_ask_user_answer = getattr(context, "on_ask_user_answer_callback", None)
        channels: list[Any] = []
        seen_paths: dict[str, str] = {}
        channel_cls = public.WhatsAppChannel
        for name, cfg in configs.items():
            connection_name = name
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
                channel_cls(
                    connection_name=connection_name,
                    auth_db_path=str(auth_db_path),
                    on_message=on_message,
                    on_chat_metadata=on_chat_metadata,
                    workspaces=workspaces,
                    on_ask_user_answer=on_ask_user_answer,
                )
            )
        return channels
