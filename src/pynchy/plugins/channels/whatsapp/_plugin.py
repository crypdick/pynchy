"""WhatsApp channel plugin implementation."""

from __future__ import annotations

import sys
from collections.abc import (
    Callable,  # noqa: TC003, RUF100 - beartype resolves this runtime annotation.
)
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

import pluggy

from pynchy.config.settings import (
    Settings,  # noqa: TC001, RUF100 - beartype resolves this runtime annotation.
)
from pynchy.logger import logger
from pynchy.plugins.channel_runtime import (  # noqa: TC001, RUF100 - beartype resolves hook annotations at runtime.
    ChannelPluginContext,
)
from pynchy.types import Channel  # noqa: TC001, RUF100 - beartype resolves annotations at runtime.

hookimpl = pluggy.HookimplMarker("pynchy")
_DUPLICATE_AUTH_DB_PATH = (
    "WhatsApp auth_db_path must be unique per connection: "
    "{first_name} and {second_name} both use {path}"
)


@runtime_checkable
class _WhatsAppPublicModule(Protocol):
    WhatsAppChannel: Callable[..., Channel]

    def get_settings(self) -> Settings: ...


def _public_module() -> _WhatsAppPublicModule:
    return cast("_WhatsAppPublicModule", sys.modules[__package__])


class WhatsAppPlugin:
    """Plugin implementing selected pynchy hooks."""

    @hookimpl
    def pynchy_create_channel(self, context: ChannelPluginContext | None) -> list[Channel] | None:
        public = _public_module()
        s: Settings = public.get_settings()
        configs = {name: cfg for name, cfg in s.connections.items() if cfg.type == "whatsapp"}
        if not configs:
            logger.debug("WhatsApp channel skipped — no connections configured")
            return None
        if context is None:
            return None
        channels: list[Channel] = []
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
                    assistant_name=s.agent.name,
                    on_message=context.on_message_callback,
                    on_chat_metadata=context.on_chat_metadata_callback,
                    workspaces=context.workspaces,
                    on_ask_user_answer=context.on_ask_user_answer_callback,
                )
            )
        return channels
