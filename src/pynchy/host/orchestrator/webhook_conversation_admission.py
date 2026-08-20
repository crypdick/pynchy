"""Atomic admission and deferred processing for routed webhook deliveries."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from pynchy.conversation.api import (
    ConversationClaimId,
    ConversationDelivery,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.host.orchestrator.webhook_event_payloads import (
    webhook_event_from_payload,
    webhook_event_payload,
)
from pynchy.host.orchestrator.webhook_event_rendering import (
    event_is_human_derived,
    event_public_source,
    prompt_for_event,
)
from pynchy.identifiers import GroupFolder
from pynchy.plugins.api import (  # noqa: TC001 - beartype resolves processing annotations at runtime.
    WebhookEvent,
    WebhookRoute,
)
from pynchy.state.api import WebhookConversationRequest


def conversation_admission_request(
    route: WebhookRoute,
    event: WebhookEvent,
    prompt: str | None,
    *,
    defer_process_event: bool = False,
) -> WebhookConversationRequest | None:
    """Build the provider-neutral FIFO envelope for one parsed event."""
    target = event.conversation
    if target is None:
        return None
    workspace = target.workspace or route.workspace
    if workspace is None:
        raise RuntimeError("Routed webhook conversation has no workspace owner")
    if event.lifecycle is None and prompt is None:
        raise ValueError("Routed webhook delivery has no prompt")
    payload: dict[str, object] = {
        "control_title": target.control_title,
        "control_closed": target.control_closed,
        "control_state_revision": target.control_state_revision,
        "event_type": event.event_type,
        "event_action": event.action,
        "human_derived": event_is_human_derived(event),
        "public_source": (
            target.public_source if target.public_source is not None else route.public_source
        ),
    }
    if event.lifecycle is not None:
        payload.update(
            {
                "delivery_mode": "lifecycle",
                "lifecycle_context": event.lifecycle.context,
                "subject_id": event.subject_id,
            }
        )
    else:
        payload["prompt"] = prompt
    if defer_process_event:
        payload["deferred_process_event"] = webhook_event_payload(event)
    return WebhookConversationRequest(
        identity=ExternalDeliveryIdentity(
            provider=ExternalProvider(route.provider),
            route=ExternalRoute(route.name),
            delivery_id=ExternalDeliveryId(event.delivery_id),
        ),
        subject=target.subject,
        workspace=GroupFolder(workspace),
        payload=payload,
        control_closed=target.control_closed,
        control_state_revision=target.control_state_revision,
    )


async def process_deferred_event(
    delivery: ConversationDelivery,
    _claim_id: ConversationClaimId,
    route: WebhookRoute,
) -> ConversationDelivery | None:
    """Run a held event's trusted processor when its FIFO claim becomes head."""
    payload = delivery.payload or {}
    raw_event = payload.get("deferred_process_event")
    if raw_event is None:
        return delivery
    if not isinstance(raw_event, Mapping):
        raise TypeError("Deferred webhook event payload is not an object")
    if route.process_event is None:
        raise RuntimeError("Deferred webhook route lost its trusted processor")
    processed = await route.process_event(webhook_event_from_payload(raw_event))
    if processed.ignored_reason is not None:
        # Completion triggers the dispatcher control projection; never turn an
        # ignored controller-owned result into an ordinary agent message.
        return None
    if processed.conversation is None:
        raise TypeError("Deferred webhook processor produced an unroutable event")
    if processed.lifecycle is not None:
        raise TypeError("Deferred nonterminal webhook became a lifecycle delivery")
    updated_payload = {
        **payload,
        "control_title": processed.conversation.control_title,
        "control_closed": processed.conversation.control_closed,
        "control_state_revision": processed.conversation.control_state_revision,
        "human_derived": event_is_human_derived(processed),
        "public_source": event_public_source(route, processed),
        "prompt": prompt_for_event(route, processed),
    }
    updated_payload.pop("deferred_process_event", None)
    return replace(delivery, payload=updated_payload)
