"""Render already-authenticated webhook events for routed agent delivery."""

from __future__ import annotations

import json
from collections.abc import Mapping

from pynchy.content_fencing import fence_untrusted_content
from pynchy.plugins.api import (
    WebhookEvent,
    WebhookRoute,
)


def event_public_source(route: WebhookRoute, event: WebhookEvent) -> bool:
    """Resolve route trust after any trusted event processing."""
    if event.conversation is not None and event.conversation.public_source is not None:
        return event.conversation.public_source
    return route.public_source


def event_is_human_derived(event: WebhookEvent) -> bool:
    """Return whether provider authentication identifies a human actor."""
    return event.actor is not None and event.actor.kind.casefold() == "user"


def prompt_for_event(route: WebhookRoute, event: WebhookEvent) -> str:
    """Render provider context under the route's declared trust boundary."""
    if event.instructions is None or event.external_context is None:
        raise ValueError("Actionable webhook event lost its prompt context")
    external_context = event.external_context
    if isinstance(external_context, Mapping):
        external_context = json.dumps(external_context, sort_keys=True, ensure_ascii=False)
    if event_public_source(route, event):
        external_context = fence_untrusted_content(
            external_context,
            source=f"{route.provider}-webhook",
        )
    return f"{event.instructions}\n\n{external_context}"
