"""WhatsApp channel plugin implementation."""

from __future__ import annotations

import sys
from collections.abc import (
    Callable,  # noqa: TC003, RUF100 - beartype resolves this runtime annotation.
)
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

import pluggy

from pynchy.config.models import (  # noqa: TC001, RUF100 - beartype resolves plugin config annotations at runtime.
    WhatsAppConnectionConfig,
)
from pynchy.config.settings import (
    Settings,  # noqa: TC001, RUF100 - beartype resolves this runtime annotation.
)
from pynchy.logger import logger

hookimpl = pluggy.HookimplMarker("pynchy")
_DUPLICATE_AUTH_DB_PATH = (
    "WhatsApp auth_db_path must be unique per connection: "
    "{first_name} and {second_name} both use {path}"
)


@runtime_checkable
class _WhatsAppPublicModule(Protocol):
    WhatsAppChannel: Callable[..., object]

    def get_settings(self) -> Settings: ...


def _public_module() -> _WhatsAppPublicModule:
    return cast("_WhatsAppPublicModule", sys.modules[__package__])


class WhatsAppPlugin:
    """Plugin implementing selected pynchy hooks."""

    @hookimpl
    def pynchy_create_channel(
        self, context: object | None
    ) -> list[object] | None:
        public = _public_module()
        s: Settings = public.get_settings()
        configs = {
            name: cast("WhatsAppConnectionConfig", cfg)
            for name, cfg in s.connections.items()
            if cfg.type == "whatsapp"
        }
        if not configs:
            logger.debug("WhatsApp channel skipped — no connections configured")
            return None
        if context is None:
            return None
        on_message = getattr(context, "on_message_callback", None)
        on_chat_metadata = getattr(context, "on_chat_metadata_callback", None)
        workspaces = getattr(context, "workspaces", None)
        if on_message is None or on_chat_metadata is None or workspaces is None:
            return None
        on_ask_user_answer = getattr(context, "on_ask_user_answer_callback", None)
        channels: list[object] = []
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
                    _DUPLICATE_AUTH_DB_PATH.format(
                        first_name=seen_paths[key],
                        second_name=name,
                        path=key,
                    )
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
