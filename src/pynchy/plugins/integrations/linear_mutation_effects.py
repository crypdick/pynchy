"""Linear mutation-side contract for durable webhook-effect correlation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pynchy.webhook_effects import (
    WebhookEffectEvidence,
    WebhookEffectId,
    WebhookEffectScope,
)

EffectBegin = Callable[[WebhookEffectScope], Awaitable[WebhookEffectId]]
EffectExecuting = Callable[[WebhookEffectId], Awaitable[None]]
EffectConfirm = Callable[[WebhookEffectId, WebhookEffectEvidence], Awaitable[None]]
EffectResolve = Callable[[WebhookEffectId], Awaitable[None]]


@dataclass(frozen=True)
class LinearSelfEchoRecorder:
    """Host-owned callbacks for the generic durable webhook-effect ledger."""

    account_name: str
    begin: EffectBegin
    mark_executing: EffectExecuting
    confirm: EffectConfirm
    fail: EffectResolve
    mark_outcome_unknown: EffectResolve


@dataclass
class LinearWebhookEffectAttempt:
    """One provider mutation whose callback must not outrun its response."""

    recorder: LinearSelfEchoRecorder | None
    effect_id: WebhookEffectId | None
    resolved: bool = False

    @property
    def account_name(self) -> str | None:
        """Return the bound account only when durable correlation is enabled."""
        return self.recorder.account_name if self.recorder is not None else None

    async def confirm(self, evidence: WebhookEffectEvidence | None) -> None:
        """Commit exact provider evidence before allowing the mutation to return."""
        if self.recorder is None or self.effect_id is None:
            return
        if evidence is None:
            raise ValueError("Durable Linear effect confirmation requires evidence")
        await self.recorder.confirm(self.effect_id, evidence)
        self.resolved = True

    async def fail(self) -> None:
        """Release candidates after an explicit provider-declared failure."""
        if self.recorder is None or self.effect_id is None:
            return
        await self.recorder.fail(self.effect_id)
        self.resolved = True
