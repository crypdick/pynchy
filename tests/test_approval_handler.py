"""Tests for approval command handling in the chat pipeline."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pynchy.host.orchestrator.messaging.approval_handler import (
    handle_approval_command,
    handle_pending_query,
)
from pynchy.host.orchestrator.messaging.deps import ApprovalRuntimeOperations


class FakeDeps:
    """Minimal deps for testing approval command semantics."""

    def __init__(self, pending: dict[str, object] | None = None) -> None:
        self.broadcast_host_message = AsyncMock()
        self.find_pending_by_short_id = MagicMock(return_value=pending)
        self.list_pending_approvals = MagicMock(return_value=[])
        self.persist_and_process = AsyncMock()
        self.approval_runtime_operations = ApprovalRuntimeOperations(
            find_pending_by_short_id=self.find_pending_by_short_id,
            list_pending_approvals=self.list_pending_approvals,
            persist_and_process=self.persist_and_process,
        )


@pytest.fixture
def pending() -> dict[str, object]:
    return {
        "request_id": "aabb001122334455",
        "guarded_action_id": "action-123",
        "request_payload_hash": "payload-456",
        "source_group": "grp",
        "approval_chat_jid": "j@g.us",
        "tool_name": "x_post",
        "short_id": "ab",
    }


class TestHandleApprovalCommand:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("action", "approved"), [("approve", True), ("deny", False)])
    async def test_persists_and_processes_matched_decision(
        self, pending: dict[str, object], action: str, approved: bool
    ) -> None:
        deps = FakeDeps(pending)

        await handle_approval_command(deps, "j@g.us", action, "ab", "testuser")

        deps.persist_and_process.assert_awaited_once()
        source_group, decision = deps.persist_and_process.call_args.args
        assert source_group == "grp"
        assert decision == {
            "request_id": "aabb001122334455",
            "guarded_action_id": "action-123",
            "request_payload_hash": "payload-456",
            "source_group": "grp",
            "approved": approved,
            "decided_by": "testuser",
            "decided_at": decision["decided_at"],
        }
        deps.broadcast_host_message.assert_awaited_once_with(
            "j@g.us", f"✅ {'Approved' if approved else 'Denied'}: x_post (ab)"
        )

    @pytest.mark.asyncio
    async def test_rejects_unknown_or_wrong_chat(self, pending: dict[str, object]) -> None:
        for deps, chat_jid in ((FakeDeps(), "j@g.us"), (FakeDeps(pending), "other@g.us")):
            await handle_approval_command(deps, chat_jid, "approve", "ab", "testuser")

            deps.persist_and_process.assert_not_awaited()
            deps.broadcast_host_message.assert_awaited_once_with(
                chat_jid, "No pending approval found for ID: ab"
            )


class TestHandlePendingQuery:
    @pytest.mark.asyncio
    async def test_lists_pending_approvals(self) -> None:
        deps = FakeDeps()
        deps.list_pending_approvals.return_value = [
            {"tool_name": "x_post", "short_id": "ab", "source_group": "grp"},
            {"tool_name": "send_email", "short_id": "cd", "source_group": "other"},
        ]

        await handle_pending_query(deps, "j@g.us")

        deps.broadcast_host_message.assert_awaited_once_with(
            "j@g.us", "Pending approvals:\n\n  • x_post (ab) — grp\n  • send_email (cd) — other"
        )

    @pytest.mark.asyncio
    async def test_no_pending_shows_message(self) -> None:
        deps = FakeDeps()

        await handle_pending_query(deps, "j@g.us")

        deps.broadcast_host_message.assert_awaited_once_with("j@g.us", "No pending approvals.")
