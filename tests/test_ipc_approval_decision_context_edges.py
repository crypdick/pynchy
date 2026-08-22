"""Fail-closed validation coverage for persisted approval decisions."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from conftest import make_host_action_catalog

from pynchy.host.container_manager.ipc.approval_decision_context import (
    ApprovalDecision,
    build_approval_decision_context,
)


def test_approval_decision_parser_rejects_malformed_fields() -> None:
    valid = {
        "request_id": "request",
        "guarded_action_id": "action",
        "request_payload_hash": "hash",
        "source_group": "group",
        "approved": True,
        "decided_by": "operator",
        "decided_at": "2026-07-29T00:00:00+00:00",
    }
    cases = (
        (None, "must be a JSON object"),
        ({**valid, "request_id": ""}, "request_id"),
        ({**valid, "decided_at": ""}, "decided_at"),
        ({**valid, "guarded_action_id": None}, "guarded_action_id"),
        ({**valid, "decided_at": "not-a-timestamp"}, "ISO timestamp"),
        ({**valid, "approval_scope": "later"}, "approval_scope"),
        ({**valid, "approved": False, "approval_scope": "once"}, "denied"),
    )

    for value, message in cases:
        with pytest.raises((TypeError, ValueError), match=message):
            ApprovalDecision.parse(value)


def test_approval_context_rejects_malformed_pending_state() -> None:
    decision = ApprovalDecision(
        request_id="request",
        approved=True,
        decided_by="operator",
        decided_at="2026-07-29T00:00:00+00:00",
    )
    base = {
        "tool_name": "tool",
        "approval_chat_jid": "j@g.us",
        "request_data": {"type": "ipc:tool"},
        "handler_type": "ipc",
    }
    cases = (
        ({**base, "request_data": []}, "request_data"),
        ({**base, "handler_type": "unknown"}, "handler_type"),
        ({**base, "origin_conversation_id": 1}, "origin_conversation_id"),
        ({**base, "action_payload": []}, "action_payload"),
        ({**base, "tool_name": ""}, "tool_name"),
    )

    for pending, message in cases:
        with pytest.raises((TypeError, ValueError), match=message):
            build_approval_decision_context(
                pending,
                decision,
                source_group="group",
                replay_gate=MagicMock(),
            )


def test_reusable_approval_requires_matching_pending_capability() -> None:
    decision = ApprovalDecision(
        request_id="request",
        approved=True,
        decided_by="operator",
        decided_at="2026-07-29T00:00:00+00:00",
        approval_scope="session",
    )
    catalog = make_host_action_catalog("tool", handler=MagicMock())
    action = catalog.action_for("tool")
    assert action is not None
    pending = {
        "tool_name": "tool",
        "approval_chat_jid": "j@g.us",
        "request_data": {"type": "service:tool"},
        "handler_type": "service",
        "allow_remember": True,
        "capability_id": str(action.capability.id),
    }

    with patch(
        "pynchy.host.container_manager.ipc.approval_decision_context._get_action_catalog",
        return_value=catalog,
    ):
        context = build_approval_decision_context(
            pending,
            decision,
            source_group="group",
            replay_gate=MagicMock(return_value=None),
        )
    assert context.approval_scope == "session"

    pending["capability_id"] = "wrong.capability"
    with (
        patch(
            "pynchy.host.container_manager.ipc.approval_decision_context._get_action_catalog",
            return_value=catalog,
        ),
        pytest.raises(ValueError, match="does not support reusable approval"),
    ):
        build_approval_decision_context(
            pending,
            decision,
            source_group="group",
            replay_gate=MagicMock(return_value=None),
        )


def test_mcp_proxy_reusable_approval_uses_persisted_capability() -> None:
    capability_id = "mcp.linear.linear_get_issue"
    context = build_approval_decision_context(
        {
            "tool_name": "linear_get_issue",
            "approval_chat_jid": "discord:channel:1",
            "request_data": {"params": {"name": "linear_get_issue"}},
            "handler_type": "mcp_proxy",
            "allow_remember": True,
            "capability_id": capability_id,
        },
        ApprovalDecision(
            request_id="request",
            approved=True,
            decided_by="operator",
            decided_at="2026-07-29T00:00:00+00:00",
            approval_scope="session",
        ),
        source_group="group",
        replay_gate=MagicMock(return_value=None),
    )

    assert context.capability_id == capability_id
    assert context.approval_scope == "session"
