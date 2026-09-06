"""Host-owned durable effect correlation for Linear mutations."""

from __future__ import annotations

from collections.abc import (
    Awaitable,
    Callable,
)
from dataclasses import dataclass

from pynchy.conversation.api import notify_conversation_delivery_completed
from pynchy.plugins.integrations.linear_client import LinearSelfEchoRecorder
from pynchy.webhook_effects import (
    WebhookEffectEvidence,
    WebhookEffectId,
    WebhookEffectResolution,
    WebhookEffectScope,
)


@dataclass(frozen=True)
class LinearSelfEchoRuntime:
    """Durable effect-ledger operations selected during plugin composition."""

    begin: Callable[[WebhookEffectScope], Awaitable[WebhookEffectId]]
    mark_executing: Callable[[WebhookEffectId], Awaitable[None]]
    confirm: Callable[[WebhookEffectId, WebhookEffectEvidence], Awaitable[WebhookEffectResolution]]
    fail: Callable[[WebhookEffectId], Awaitable[WebhookEffectResolution]]
    mark_outcome_unknown: Callable[[WebhookEffectId], Awaitable[None]]


_runtime: LinearSelfEchoRuntime | None = None


def configure_linear_self_echo_runtime(runtime: LinearSelfEchoRuntime) -> None:
    """Set the durable ledger operations used for Linear self-echo correlation."""
    global _runtime  # noqa: PLW0603 - one host process owns this configured runtime.
    _runtime = runtime


def _configured_runtime() -> LinearSelfEchoRuntime:
    if _runtime is None:
        raise RuntimeError("Linear self-echo runtime has not been configured")
    return _runtime


async def _notify_resolution(resolution: WebhookEffectResolution) -> None:
    for wakeup in resolution.wakeups:
        await notify_conversation_delivery_completed(wakeup)


async def _confirm(
    effect_id: WebhookEffectId,
    evidence: WebhookEffectEvidence,
) -> None:
    await _notify_resolution(await _configured_runtime().confirm(effect_id, evidence))


async def _fail(effect_id: WebhookEffectId) -> None:
    await _notify_resolution(await _configured_runtime().fail(effect_id))


def linear_self_echo_recorder(account_name: str) -> LinearSelfEchoRecorder:
    """Bind the generic durable effect ledger to one Linear account."""
    runtime = _configured_runtime()
    return LinearSelfEchoRecorder(
        account_name=account_name,
        begin=runtime.begin,
        mark_executing=runtime.mark_executing,
        confirm=_confirm,
        fail=_fail,
        mark_outcome_unknown=runtime.mark_outcome_unknown,
    )
