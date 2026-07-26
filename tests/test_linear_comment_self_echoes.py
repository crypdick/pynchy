"""Durable exact correlation for Pynchy-created Linear comments."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pynchy.actions import ACTION_SPECS
from pynchy.capabilities import validate_host_action_descriptors
from pynchy.plugins.integrations.linear_accounts import LinearAccount
from pynchy.plugins.integrations.linear_client import LinearClient, LinearSelfEchoRecorder
from pynchy.plugins.integrations.linear_comment_actions import handle_create_comment
from pynchy.plugins.integrations.linear_config import LinearTool
from pynchy.plugins.integrations.linear_work_item_actions import host_action_registration
from pynchy.plugins.integrations.linear_work_item_provider import LinearClientContext
from pynchy.state import (
    LinearCommentSelfEcho,
    WebhookReceipt,
    admit_webhook_receipt,
    get_webhook_receipt,
    init_test_database,
    record_linear_comment_self_echo,
)

_RECEIVED_AT = "2026-07-26T00:00:00+00:00"
_REVISION = "2026-07-26T00:00:01+00:00"


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


def _receipt(delivery_id: str) -> WebhookReceipt:
    return WebhookReceipt(
        provider="linear",
        route="project",
        delivery_id=delivery_id,
        workspace="project",
        event_type="Comment",
        event_action="create",
        subject_id="issue-1",
        payload_sha256=f"payload-{delivery_id}",
        disposition="routed",
        ignored_reason=None,
        task_id=None,
        occurred_at=_RECEIVED_AT,
        received_at=_RECEIVED_AT,
    )


def _self_echo(*, revision: str = _REVISION) -> LinearCommentSelfEcho:
    return LinearCommentSelfEcho(
        account_name="linear-project",
        comment_id="comment-1",
        issue_id="issue-1",
        revision=revision,
    )


class _LinearClientContext:
    def __init__(self, client: MagicMock) -> None:
        self._client = client

    async def __aenter__(self) -> MagicMock:
        return self._client

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        return None


async def test_exact_marker_is_consumed_with_ignored_webhook_receipt() -> None:
    marker = _self_echo()
    await record_linear_comment_self_echo(marker)

    admitted = await admit_webhook_receipt(
        _receipt("delivery-1"),
        None,
        self_echo=marker,
    )

    assert admitted.created is True
    assert admitted.task is None
    assert admitted.self_echo_suppressed is True
    assert admitted.receipt.disposition == "ignored"
    assert admitted.receipt.ignored_reason == "pynchy_self_comment_echo"

    stored = await get_webhook_receipt("linear", "project", "delivery-1")
    assert stored == admitted.receipt


async def test_duplicate_delivery_keeps_its_suppressed_receipt_after_marker_consumption() -> None:
    marker = _self_echo()
    await record_linear_comment_self_echo(marker)
    receipt = _receipt("delivery-1")

    first = await admit_webhook_receipt(receipt, None, self_echo=marker)
    duplicate = await admit_webhook_receipt(receipt, None, self_echo=marker)

    assert first.self_echo_suppressed is True
    assert duplicate.created is False
    assert duplicate.self_echo_suppressed is True
    assert duplicate.receipt == first.receipt


async def test_mismatched_revision_remains_actionable_and_does_not_consume_marker() -> None:
    marker = _self_echo()
    await record_linear_comment_self_echo(marker)

    mismatch = await admit_webhook_receipt(
        _receipt("delivery-mismatch"),
        None,
        self_echo=_self_echo(revision="2026-07-26T00:00:02+00:00"),
    )
    exact = await admit_webhook_receipt(
        _receipt("delivery-exact"),
        None,
        self_echo=marker,
    )

    assert mismatch.self_echo_suppressed is False
    assert mismatch.receipt.disposition == "routed"
    assert exact.self_echo_suppressed is True


async def test_comment_client_returns_and_records_exact_response_evidence() -> None:
    recorder = AsyncMock()
    client = LinearClient(
        api_key="lin_api_test",  # pragma: allowlist secret
        session=AsyncMock(),
        self_echo_recorder=LinearSelfEchoRecorder(comment_created=recorder),
    )
    client.query = AsyncMock(
        return_value={
            "commentCreate": {
                "success": True,
                "comment": {
                    "id": "comment-1",
                    "body": "Validation passed.",
                    "createdAt": _RECEIVED_AT,
                    "updatedAt": _REVISION,
                    "issue": {"id": "issue-1"},
                },
            }
        }
    )

    comment = await client.create_comment("issue-1", "Validation passed.")

    assert comment["issueId"] == "issue-1"
    assert comment["updatedAt"] == _REVISION
    recorder.assert_awaited_once_with(comment)


async def test_host_client_context_records_the_marker_used_by_reset_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    account = LinearAccount("linear-project", LinearTool(type="linear"))
    async with LinearClientContext(account) as client:
        client.query = AsyncMock(
            return_value={
                "commentCreate": {
                    "success": True,
                    "comment": {
                        "id": "comment-1",
                        "createdAt": _RECEIVED_AT,
                        "updatedAt": _REVISION,
                        "issue": {"id": "issue-1"},
                    },
                }
            }
        )
        await client.create_comment("issue-1", "Context reset blocked this attempt.")

    admitted = await admit_webhook_receipt(
        _receipt("delivery-reset-comment"),
        None,
        self_echo=_self_echo(),
    )

    assert admitted.self_echo_suppressed is True


@pytest.mark.action("linear.comment.create")
async def test_host_comment_action_preserves_workspace_and_provider_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock()
    client.create_comment = AsyncMock(
        return_value={
            "id": "comment-1",
            "issueId": "issue-1",
            "createdAt": _RECEIVED_AT,
            "updatedAt": _REVISION,
        }
    )
    workspace_issue = AsyncMock()
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_comment_actions.linear_client",
        lambda *, workspace: _LinearClientContext(client),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_comment_actions.workspace_issue",
        workspace_issue,
    )

    result = await handle_create_comment(
        {
            "source_group": "project",
            "issue_id": "issue-1",
            "body": "Validation passed.",
        }
    )

    assert result["result"] == client.create_comment.return_value
    workspace_issue.assert_awaited_once_with(client, "project", "issue-1")
    client.create_comment.assert_awaited_once_with("issue-1", "Validation passed.")


def test_comment_host_action_has_a_valid_agent_tool_catalog_surface() -> None:
    actions = host_action_registration().actions
    comment_action = next(
        action for action in actions if action.tool_name == "linear_create_comment"
    )

    assert comment_action.action_intent is not None
    assert not validate_host_action_descriptors(actions, ACTION_SPECS)
