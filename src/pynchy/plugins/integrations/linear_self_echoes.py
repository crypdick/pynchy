"""Host-owned durable effect correlation for Linear mutations."""

from __future__ import annotations

from pynchy.conversation.dispatch import notify_conversation_delivery_completed
from pynchy.plugins.integrations.linear_client import LinearSelfEchoRecorder
from pynchy.state.api import (
    begin_webhook_effect,
    confirm_webhook_effect,
    fail_webhook_effect,
    mark_webhook_effect_executing,
    mark_webhook_effect_outcome_unknown,
)
from pynchy.webhook_effects import (  # noqa: TC001, RUF100 - beartype resolves recorder callbacks.
    WebhookEffectEvidence,
    WebhookEffectId,
    WebhookEffectResolution,
)


async def _notify_resolution(resolution: WebhookEffectResolution) -> None:
    for wakeup in resolution.wakeups:
        await notify_conversation_delivery_completed(wakeup)


async def _confirm(
    effect_id: WebhookEffectId,
    evidence: WebhookEffectEvidence,
) -> None:
    await _notify_resolution(await confirm_webhook_effect(effect_id, evidence))


async def _fail(effect_id: WebhookEffectId) -> None:
    await _notify_resolution(await fail_webhook_effect(effect_id))


def linear_self_echo_recorder(account_name: str) -> LinearSelfEchoRecorder:
    """Bind the generic durable effect ledger to one Linear account."""
    return LinearSelfEchoRecorder(
        account_name=account_name,
        begin=begin_webhook_effect,
        mark_executing=mark_webhook_effect_executing,
        confirm=_confirm,
        fail=_fail,
        mark_outcome_unknown=mark_webhook_effect_outcome_unknown,
    )
