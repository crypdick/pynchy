"""Ordered schema fragments for authenticated external conversation routing."""

from pynchy.state.conversation_routing_schema import CONVERSATION_ROUTING_SCHEMA
from pynchy.state.webhook_schema import WEBHOOK_SCHEMA

EXTERNAL_ROUTING_SCHEMA = WEBHOOK_SCHEMA + CONVERSATION_ROUTING_SCHEMA
