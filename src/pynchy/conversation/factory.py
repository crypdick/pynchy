from __future__ import annotations

import os
from collections.abc import (
    Mapping,  # noqa: TC003, RUF100 - beartype resolves annotations at runtime.
)

from pynchy.config import get_settings
from pynchy.conversation.phoenix import PhoenixConversationStore, phoenix_tracer
from pynchy.conversation.sink import ConversationSink


def _normalized_endpoint(value: str | None) -> str | None:
    endpoint = value.strip().rstrip("/") if value is not None else ""
    if endpoint.endswith("/v1/traces"):
        endpoint = endpoint.removesuffix("/v1/traces").rstrip("/")
    return endpoint or None


def resolved_phoenix_endpoint(env: Mapping[str, str] | None = None) -> str | None:
    source = env if env is not None else os.environ
    base = _normalized_endpoint(source.get("PHOENIX_COLLECTOR_ENDPOINT"))
    if base:
        return base
    return _normalized_endpoint(source.get("PHOENIX_COLLECTOR_HTTP_ENDPOINT"))


def build_conversation_sink() -> ConversationSink:
    settings = get_settings().conversation_store
    endpoint = _normalized_endpoint(settings.phoenix_endpoint) or resolved_phoenix_endpoint()
    tracer = phoenix_tracer(settings.project_name, endpoint=endpoint)
    return ConversationSink(body_store=PhoenixConversationStore(tracer=tracer))
