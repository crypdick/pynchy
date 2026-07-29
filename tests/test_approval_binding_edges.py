"""Approval binding rejection contracts."""

from __future__ import annotations

from unittest.mock import patch

from pynchy.host.container_manager.security.approval_binding import approval_binding_error


def _pending() -> dict[str, object]:
    return {
        "request_data": {"command": "ls"},
        "guarded_action_id": "action-1",
        "request_payload_hash": "hash-1",
        "source_group": "group",
    }


def test_approval_rejects_non_object_pending_payload() -> None:
    pending = _pending()
    pending["request_data"] = []

    assert (
        approval_binding_error(
            pending,
            {},
            request_id="action-1",
            source_group="group",
        )
        == "Pending approval payload is not a JSON object"
    )


def test_approval_rejects_incomplete_pending_binding() -> None:
    pending = _pending()
    pending.pop("guarded_action_id")

    assert (
        approval_binding_error(
            pending,
            {},
            request_id="action-1",
            source_group="group",
        )
        == "Pending approval lacks a complete payload binding"
    )


def test_approval_rejects_incomplete_decision_binding() -> None:
    with patch(
        "pynchy.host.container_manager.security.approval_binding.payload_hash_matches",
        return_value=True,
    ):
        assert (
            approval_binding_error(
                _pending(),
                {},
                request_id="action-1",
                source_group="group",
            )
            == "Approval decision lacks the reviewed payload binding"
        )
