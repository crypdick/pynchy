"""Pre-effect classification and atomic webhook delivery admission."""

from __future__ import annotations

from dataclasses import dataclass

from pynchy.conversation.api import (
    ConversationId,
)
from pynchy.host.orchestrator.conversation_control import sync_existing_open_conversation_control
from pynchy.host.orchestrator.webhook_conversations import (
    WebhookConversationDispatcher,
)
from pynchy.host.orchestrator.webhook_event_rendering import prompt_for_event
from pynchy.plugins.api import (
    WebhookEvent,
    WebhookRoute,
)
from pynchy.scheduling.api import ScheduledTask
from pynchy.state.api import (
    WebhookAdmission,
    WebhookReceipt,
    admit_webhook_receipt,
    classify_webhook_effect_callback,
)
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
    open_conversation_id: ConversationId | None = None
    if (
        dispatcher is not None
        and event.conversation is not None
        and event.conversation.control_closed is False
    ):
        # Project a newer provider reopen before terminal retirement can retire
        # an otherwise valid FIFO delivery during its durable cleanup phase.
        open_conversation_id = await dispatcher.project_open_control(route, event)
    if event.conversation is None or receipt.disposition not in {"routed", "lifecycle"}:
        admission = await admit_webhook_receipt(
            receipt,
            request.task,
            effect_evidence=event.effect_evidence,
        )
        if (
            receipt.disposition == "ignored"
            and event.conversation is not None
            and event.conversation.control_closed is False
            and dispatcher is not None
        ):
            await sync_existing_open_conversation_control(
                dispatcher.deps.channels(),
                event.conversation.subject,
            )
            if open_conversation_id is not None:
                await dispatcher.restore_existing_open_control_runtime(open_conversation_id)
        return admission, None
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
