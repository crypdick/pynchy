"""Pre-effect classification and atomic webhook delivery admission."""

from __future__ import annotations

from dataclasses import dataclass

from pynchy.conversation.models import (  # noqa: TC001, RUF100 - beartype resolves admission results at runtime.
    ConversationId,
)
from pynchy.host.orchestrator.webhook_conversations import (  # noqa: TC001, RUF100 - beartype resolves dispatcher inputs at runtime.
    WebhookConversationDispatcher,
)
from pynchy.host.orchestrator.webhook_event_rendering import prompt_for_event
from pynchy.plugins.webhooks import (  # noqa: TC001, RUF100 - beartype resolves admission inputs at runtime.
    WebhookEvent,
    WebhookRoute,
)
from pynchy.state import (
    WebhookAdmission,
    WebhookReceipt,
    admit_webhook_receipt,
)
from pynchy.state.webhook_effect_admission import classify_webhook_effect_callback
from pynchy.types import ScheduledTask  # noqa: TC001, RUF100 - beartype resolves requests.
from pynchy.webhook_effects import WebhookEffectCallbackDecision


@dataclass(frozen=True)
class WebhookDeliveryAdmissionRequest:
    """Host objects needed to commit one prepared webhook event."""

    receipt: WebhookReceipt
    task: ScheduledTask | None
    defer_process_event: bool


async def effect_callback_decision(event: WebhookEvent) -> WebhookEffectCallbackDecision:
    """Classify correlation before invoking trusted route-owned effects."""
    evidence = event.effect_evidence
    if evidence is None or event.lifecycle is not None:
        return WebhookEffectCallbackDecision.UNRELATED
    return await classify_webhook_effect_callback(evidence, event.occurred_at)


async def admit_prepared_event(
    dispatcher: WebhookConversationDispatcher | None,
    route: WebhookRoute,
    event: WebhookEvent,
    request: WebhookDeliveryAdmissionRequest,
) -> tuple[WebhookAdmission, ConversationId | None]:
    """Commit either an isolated receipt or an atomic routed FIFO envelope."""
    receipt = request.receipt
    if event.conversation is None or receipt.disposition not in {"routed", "lifecycle"}:
        return (
            await admit_webhook_receipt(
                receipt,
                request.task,
                effect_evidence=event.effect_evidence,
            ),
            None,
        )
    if dispatcher is None:
        raise RuntimeError("Routed webhook dispatcher disappeared after startup")
    prompt = prompt_for_event(route, event) if receipt.disposition == "routed" else None
    return await dispatcher.admit_webhook(
        route,
        event,
        prompt,
        receipt,
        defer_process_event=request.defer_process_event,
    )
