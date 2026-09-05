from __future__ import annotations

import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class ConversationEventKind(StrEnum):
    USER_MESSAGE = "user_message"  # noqa: V107
    ASSISTANT_MESSAGE = "assistant_message"  # noqa: V107
    SYSTEM_NOTICE = "system_notice"  # noqa: V107


def new_turn_id() -> str:
    return f"turn_{secrets.token_urlsafe(18)}"


_WHITESPACE = re.compile(r"\s+")


def content_preview(content: str, limit: int = 500) -> str:
    normalized = _WHITESPACE.sub(" ", content).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + "..."


def _freeze_metadata_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_metadata_value(nested) for key, nested in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze_metadata_value(item) for item in value)
    return value


def _json_ready_metadata_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _json_ready_metadata_value(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_json_ready_metadata_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ConversationEvent:
    event_id: str
    turn_id: str
    chat_jid: str
    timestamp: str
    kind: ConversationEventKind
    sender: str
    sender_name: str | None
    content: str
    message_type: str
    source_message_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze_metadata_value(self.metadata))

    @property
    def preview(self) -> str:
        return content_preview(self.content)

    def span_attributes(self) -> dict[str, object]:
        attrs: dict[str, object] = {
            "pynchy.event_id": self.event_id,
            "pynchy.turn_id": self.turn_id,
            "pynchy.chat_jid": self.chat_jid,
            "pynchy.kind": self.kind.value,
            "pynchy.sender": self.sender,
            "pynchy.message_type": self.message_type,
            "pynchy.content": self.content,
            "pynchy.content_preview": self.preview,
        }
        if self.sender_name:
            attrs["pynchy.sender_name"] = self.sender_name
        if self.source_message_id:
            attrs["pynchy.source_message_id"] = self.source_message_id
        if self.metadata:
            attrs["pynchy.metadata_json"] = json.dumps(
                _json_ready_metadata_value(self.metadata),
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        return attrs
