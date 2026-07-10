from __future__ import annotations

from pynchy.config import get_settings
from pynchy.conversation.endpoints import _normalized_endpoint, resolved_phoenix_endpoint
from pynchy.conversation.phoenix import PhoenixConversationStore, phoenix_tracer
from pynchy.conversation.sink import ConversationSink


def build_conversation_sink() -> ConversationSink:
    settings = get_settings().conversation_store
    endpoint = _normalized_endpoint(settings.phoenix_endpoint) or resolved_phoenix_endpoint()
    tracer = phoenix_tracer(settings.project_name, endpoint=endpoint)
    return ConversationSink(body_store=PhoenixConversationStore(tracer=tracer))
