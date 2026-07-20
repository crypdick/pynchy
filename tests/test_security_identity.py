"""Approval identity and exact-payload receipt behavior."""

from __future__ import annotations

import pytest

from pynchy.host.container_manager.security.identity import (
    ReceiptVerification,
    clear_approval_receipts,
    consume_approval_receipt,
    guarded_action_id,
    issue_approval_receipt,
    request_payload_hash,
)


@pytest.fixture(autouse=True)
def _clear_receipts() -> None:
    clear_approval_receipts()


def test_payload_hash_is_canonical_and_excludes_only_receipt() -> None:
    first = {"type": "schedule_task", "request_id": "a1", "nested": {"b": 2, "a": 1}}
    reordered = {
        "nested": {"a": 1, "b": 2},
        "request_id": "a1",
        "type": "schedule_task",
        "_approval_receipt": "transport-only",
    }

    assert request_payload_hash(first) == request_payload_hash(reordered)
    reordered["caller_claim"] = True
    assert request_payload_hash(first) != request_payload_hash(reordered)


@pytest.mark.parametrize(
    ("workspace", "operation", "mutation"),
    [
        ("other", "schedule_task", None),
        ("admin", "register_group", None),
        ("admin", "schedule_task", ("prompt", "changed")),
    ],
)
def test_receipt_rejects_scope_or_payload_mismatch(
    workspace: str,
    operation: str,
    mutation: tuple[str, str] | None,
) -> None:
    request = {"type": "schedule_task", "request_id": "a1", "prompt": "safe"}
    receipt = issue_approval_receipt(
        action_id=guarded_action_id("a1"),
        workspace="admin",
        operation="schedule_task",
        request_data=request,
    )
    replay = {**request, "_approval_receipt": str(receipt)}
    if mutation is not None:
        replay[mutation[0]] = mutation[1]

    assert (
        consume_approval_receipt(replay, workspace=workspace, operation=operation)
        is ReceiptVerification.INVALID
    )


def test_receipt_is_single_use() -> None:
    request = {"type": "schedule_task", "request_id": "a1", "prompt": "safe"}
    receipt = issue_approval_receipt(
        action_id=guarded_action_id("a1"),
        workspace="admin",
        operation="schedule_task",
        request_data=request,
    )

    first = {**request, "_approval_receipt": str(receipt)}
    second = {**request, "_approval_receipt": str(receipt)}
    assert (
        consume_approval_receipt(first, workspace="admin", operation="schedule_task")
        is ReceiptVerification.VALID
    )
    assert (
        consume_approval_receipt(second, workspace="admin", operation="schedule_task")
        is ReceiptVerification.INVALID
    )
