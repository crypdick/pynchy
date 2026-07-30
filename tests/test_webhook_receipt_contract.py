"""Public contract tests for authenticated webhook receipts."""

from __future__ import annotations

from dataclasses import replace

import pytest

from pynchy.state import WebhookReceipt


def _ignored_receipt() -> WebhookReceipt:
    return WebhookReceipt(
        provider="linear",
        route="project",
        delivery_id="delivery-1",
        workspace="project",
        event_type="Issue",
        event_action="update",
        subject_id="issue-1",
        payload_sha256="digest",
        disposition="ignored",
        ignored_reason="unmanaged",
        task_id=None,
        occurred_at="2026-07-29T00:00:00+00:00",
        received_at="2026-07-29T00:00:01+00:00",
    )


def test_accepted_receipt_requires_task() -> None:
    with pytest.raises(ValueError, match="require exactly one task"):
        replace(_ignored_receipt(), disposition="accepted", ignored_reason=None)


def test_routed_receipt_cannot_carry_ignore_reason() -> None:
    with pytest.raises(ValueError, match="cannot create separate tasks"):
        replace(_ignored_receipt(), disposition="routed")


def test_lifecycle_receipt_cannot_carry_ignore_reason() -> None:
    with pytest.raises(ValueError, match="cannot create separate tasks"):
        replace(_ignored_receipt(), disposition="lifecycle")


def test_notified_receipt_cannot_carry_ignore_reason() -> None:
    with pytest.raises(ValueError, match="cannot create tasks or carry ignore reasons"):
        replace(_ignored_receipt(), disposition="notified")


def test_ignored_receipt_requires_a_reason() -> None:
    with pytest.raises(ValueError, match="Ignored webhook receipts require a reason"):
        replace(_ignored_receipt(), ignored_reason=None)
