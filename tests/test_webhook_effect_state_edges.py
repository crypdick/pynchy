"""Idempotency and invalid-transition contracts for webhook effects."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from pynchy.plugins.integrations.linear_webhook_evidence import comment_webhook_evidence
from pynchy.state import (
    WebhookReceipt,
    admit_webhook_receipt,
    begin_webhook_effect,
    confirm_webhook_effect,
    fail_webhook_effect,
    list_webhook_effects,
    mark_webhook_effect_executing,
    mark_webhook_effect_outcome_unknown,
    reconcile_webhook_effect_absent,
)
from pynchy.webhook_effects import WebhookEffectId, WebhookEffectScope

pytest_plugins = ("tests.state_support",)


def _scope() -> WebhookEffectScope:
    return WebhookEffectScope(
        provider="linear",
        account="linear-project",
        event_type="Comment",
        event_action="create",
        subject_id="issue-1",
    )


def _evidence(revision: str = "2026-07-29T00:00:00+00:00"):
    return comment_webhook_evidence(
        "linear-project",
        comment_id="comment-1",
        issue_id="issue-1",
        revision=revision,
    )


def _receipt() -> WebhookReceipt:
    occurred_at = datetime.now(UTC).isoformat()
    return WebhookReceipt(
        provider="linear",
        route="project",
        delivery_id="candidate-without-conversation",
        workspace="project",
        event_type="Comment",
        event_action="create",
        subject_id="issue-1",
        payload_sha256="payload",
        disposition="routed",
        ignored_reason=None,
        task_id=None,
        occurred_at=occurred_at,
        received_at=occurred_at,
    )


async def test_effect_rejects_unknown_and_unprepared_transitions() -> None:
    with pytest.raises(ValueError, match="Unknown webhook effect"):
        await confirm_webhook_effect(WebhookEffectId("missing"), _evidence())

    effect_id = await begin_webhook_effect(_scope())
    with pytest.raises(ValueError, match="cannot be confirmed from prepared"):
        await confirm_webhook_effect(effect_id, _evidence())


async def test_effect_execution_is_not_started_twice() -> None:
    effect_id = await begin_webhook_effect(_scope())
    await mark_webhook_effect_executing(effect_id)

    with pytest.raises(ValueError, match="not prepared"):
        await mark_webhook_effect_executing(effect_id)


async def test_confirmed_effect_is_idempotent_and_rejects_new_evidence() -> None:
    effect_id = await begin_webhook_effect(_scope())
    await mark_webhook_effect_executing(effect_id)
    await confirm_webhook_effect(effect_id, _evidence())

    assert (await confirm_webhook_effect(effect_id, _evidence())).wakeups == ()
    with pytest.raises(ValueError, match="different evidence"):
        await confirm_webhook_effect(effect_id, _evidence("2026-07-29T00:00:01+00:00"))


async def test_confirmation_rejects_evidence_for_a_different_scope() -> None:
    effect_id = await begin_webhook_effect(_scope())
    await mark_webhook_effect_executing(effect_id)
    mismatched = replace(_evidence(), scope=replace(_scope(), subject_id="issue-2"))

    with pytest.raises(ValueError, match="does not match its intent"):
        await confirm_webhook_effect(effect_id, mismatched)


async def test_failed_effect_is_idempotent_and_cannot_follow_confirmation() -> None:
    effect_id = await begin_webhook_effect(_scope())
    assert (await fail_webhook_effect(effect_id)).wakeups == ()
    assert (await fail_webhook_effect(effect_id)).wakeups == ()

    confirmed = await begin_webhook_effect(_scope())
    await mark_webhook_effect_executing(confirmed)
    await confirm_webhook_effect(confirmed, _evidence())
    with pytest.raises(ValueError, match="cannot become failed from confirmed"):
        await fail_webhook_effect(confirmed)


async def test_unknown_effect_is_idempotent_and_requires_execution() -> None:
    effect_id = await begin_webhook_effect(_scope())
    with pytest.raises(ValueError, match="cannot be unknown from prepared"):
        await mark_webhook_effect_outcome_unknown(effect_id)

    await mark_webhook_effect_executing(effect_id)
    await mark_webhook_effect_outcome_unknown(effect_id)
    await mark_webhook_effect_outcome_unknown(effect_id)


async def test_absent_effect_without_a_conversation_has_no_wakeup() -> None:
    effect_id = await begin_webhook_effect(_scope())
    await mark_webhook_effect_executing(effect_id)
    admission = await admit_webhook_receipt(_receipt(), None, effect_evidence=_evidence())
    assert admission.outbound_effect_held is True

    await mark_webhook_effect_outcome_unknown(effect_id)
    resolution = await reconcile_webhook_effect_absent(effect_id)

    assert resolution.wakeups == ()


async def test_effect_listing_supports_default_limit() -> None:
    await begin_webhook_effect(_scope())

    assert len(await list_webhook_effects()) == 1
    with pytest.raises(ValueError, match="from 1 to 200"):
        await list_webhook_effects(limit=0)
