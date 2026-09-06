"""WhatsApp channel plugin implementation."""

from __future__ import annotations

import sys
from collections.abc import (
    Callable,
)
from typing import Protocol, cast, runtime_checkable

import pluggy

from pynchy.logger import logger
from pynchy.plugins.api import (
    Channel,  # beartype resolves annotations at runtime.
    ChannelPluginContext,
)

hookimpl = pluggy.HookimplMarker("pynchy")
_DUPLICATE_AUTH_DB_PATH = (
    "WhatsApp auth_db_path must be unique per connection: "
    "{first_name} and {second_name} both use {path}"
)


@runtime_checkable
class _WhatsAppPublicModule(Protocol):
    WhatsAppChannel: Callable[..., Channel]


def _public_module() -> _WhatsAppPublicModule:
    return cast("_WhatsAppPublicModule", sys.modules[__package__])


class WhatsAppPlugin:
    """Plugin implementing selected pynchy hooks."""

    @hookimpl
    def pynchy_create_channel(self, context: ChannelPluginContext | None) -> list[Channel] | None:
        if context is None:
            return None
        if not context.whatsapp_connections:
            logger.debug("WhatsApp channel skipped — no connections configured")
            return None
        channels: list[Channel] = []
        seen_paths: dict[str, str] = {}
        channel_cls = _public_module().WhatsAppChannel
        for name, settings in context.whatsapp_connections.items():
            connection_name = name
            auth_db_path = settings.auth_db_path
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
                    assistant_name=settings.assistant_name,
                    on_message=context.on_message_callback,
                    on_chat_metadata=context.on_chat_metadata_callback,
                    workspaces=context.workspaces,
                    on_ask_user_answer=context.on_ask_user_answer_callback,
                    find_chat_jids_by_name=context.find_chat_jids_by_name,
                    get_last_group_sync=context.get_last_group_sync,
                    set_last_group_sync=context.set_last_group_sync,
                    update_chat_name=context.update_chat_name,
                )
            )
        return channels
