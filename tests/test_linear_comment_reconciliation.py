"""Behavioral tests for unknown Linear comment outcomes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pynchy.action_intents import ActionIntent, ActionIntentStatus
from pynchy.plugins.integrations.linear_comment_actions import (
    linear_comment_execution_data,
    reconcile_unknown_linear_comment,
)

_ISSUE_ID = "issue-1"
_RECEIVED_AT = "2026-07-26T16:00:00+00:00"
_REVISION = "2026-07-26T16:00:01+00:00"


class _LinearClientContext:
    def __init__(self, client: MagicMock) -> None:
        self._client = client

    async def __aenter__(self) -> MagicMock:
        return self._client

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        return None


def _unknown_comment_intent() -> ActionIntent:
    return ActionIntent(
        id="intent-1",
        request_id="request-1",
        workspace="project",
        action_id="linear.comment.create",
        tool_name="linear_create_comment",
        provider="linear",
        actor_jid="project@g.us",
        recipient=f"linear:project:issue:{_ISSUE_ID}",
        payload={"issue_id": _ISSUE_ID, "body": "Validation passed."},
        source_refs=(),
        summary="Comment on Linear issue issue-1",
        policy_decision="approved",
        approver="test-user",
        approved_at=_RECEIVED_AT,
        status=ActionIntentStatus.OUTCOME_UNKNOWN,
        claimed_at=_RECEIVED_AT,
        execution_started_at=_RECEIVED_AT,
        attempts=1,
        provider_request_id=None,
        provider_receipt=None,
        error="Linear request failed",
        created_at=_RECEIVED_AT,
        updated_at=_RECEIVED_AT,
        resolved_at=_RECEIVED_AT,
    )


def test_comment_execution_data_adds_a_request_bound_hidden_marker() -> None:
    prepared = linear_comment_execution_data(
        {"source_group": "project", "issue_id": _ISSUE_ID, "body": "Validation passed."},
        "request-1",
    )

    assert prepared["body"] == "Validation passed.\n\n<!-- pynchy-action-intent:request-1 -->"


@pytest.mark.parametrize("copies", [0, 2])
async def test_comment_reconciliation_leaves_absent_or_duplicate_markers_unknown(
    monkeypatch: pytest.MonkeyPatch,
    copies: int,
) -> None:
    client = MagicMock()
    expected = "Validation passed.\n\n<!-- pynchy-action-intent:request-1 -->"
    client.list_issue_comments = AsyncMock(
        return_value=[
            {
                "id": f"comment-{index}",
                "body": expected,
                "issueId": _ISSUE_ID,
                "createdAt": _RECEIVED_AT,
                "updatedAt": _REVISION,
            }
            for index in range(copies)
        ]
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_comment_actions.linear_client",
        lambda *, workspace: _LinearClientContext(client),
    )

    receipt = await reconcile_unknown_linear_comment(_unknown_comment_intent())

    assert receipt is None
    client.list_issue_comments.assert_awaited_once_with(_ISSUE_ID)


async def test_comment_reconciliation_returns_the_exact_marked_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock()
    expected = "Validation passed.\n\n<!-- pynchy-action-intent:request-1 -->"
    client.list_issue_comments = AsyncMock(
        return_value=[
            {
                "id": "comment-1",
                "body": expected,
                "issueId": _ISSUE_ID,
                "createdAt": _RECEIVED_AT,
                "updatedAt": _REVISION,
            }
        ]
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_comment_actions.linear_client",
        lambda *, workspace: _LinearClientContext(client),
    )

    receipt = await reconcile_unknown_linear_comment(_unknown_comment_intent())

    assert receipt is not None
    assert receipt.provider_request_id == "comment-1"
    assert receipt.receipt["body"] == expected
