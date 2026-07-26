"""Durable exact correlation for Pynchy-created Linear state updates."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pynchy.plugins.integrations.linear_accounts import LinearAccount
from pynchy.plugins.integrations.linear_client import LinearClient, LinearSelfEchoRecorder
from pynchy.plugins.integrations.linear_config import LinearTool
from pynchy.plugins.integrations.linear_plans import update_issue_plan
from pynchy.plugins.integrations.linear_work_item_provider import (
    LinearClientContext,
    update_issue_state,
)
from pynchy.state import (
    LinearIssueStateSelfEcho,
    WebhookReceipt,
    admit_webhook_receipt,
    get_webhook_receipt,
    init_test_database,
    record_linear_issue_state_self_echo,
)

_RECEIVED_AT = "2026-07-26T00:00:00+00:00"
_REVISION = "2026-07-26T00:00:01+00:00"
_ISSUE_ID = "issue-1"
_STATE_ID = "state-awaiting-review"


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


def _receipt(delivery_id: str) -> WebhookReceipt:
    return WebhookReceipt(
        provider="linear",
        route="project",
        delivery_id=delivery_id,
        workspace="project",
        event_type="Issue",
        event_action="update",
        subject_id=_ISSUE_ID,
        payload_sha256=f"payload-{delivery_id}",
        disposition="routed",
        ignored_reason=None,
        task_id=None,
        occurred_at=_RECEIVED_AT,
        received_at=_RECEIVED_AT,
    )


def _self_echo(
    *,
    state_id: str = _STATE_ID,
    revision: str = _REVISION,
) -> LinearIssueStateSelfEcho:
    return LinearIssueStateSelfEcho(
        account_name="linear-project",
        issue_id=_ISSUE_ID,
        state_id=state_id,
        revision=revision,
    )


def _issue(*, state_type: str = "started") -> dict[str, object]:
    return {
        "id": _ISSUE_ID,
        "updatedAt": _REVISION,
        "state": {"id": _STATE_ID, "type": state_type},
    }


class _QueryOnlyIssueClient:
    """Provider stub intentionally without the host-owned self-echo recorder."""

    async def query(self, _query: str, **_variables: object) -> dict[str, object]:
        return {"issueUpdate": {"success": True, "issue": _issue()}}


async def test_exact_state_marker_is_consumed_with_ignored_webhook_receipt() -> None:
    marker = _self_echo()
    await record_linear_issue_state_self_echo(marker)

    admitted = await admit_webhook_receipt(
        _receipt("delivery-1"),
        None,
        self_echo=marker,
    )

    assert admitted.created is True
    assert admitted.task is None
    assert admitted.self_echo_suppressed is True
    assert admitted.receipt.disposition == "ignored"
    assert admitted.receipt.ignored_reason == "pynchy_self_issue_state_echo"

    stored = await get_webhook_receipt("linear", "project", "delivery-1")
    assert stored == admitted.receipt


async def test_mismatched_state_evidence_remains_actionable_and_preserves_marker() -> None:
    marker = _self_echo()
    await record_linear_issue_state_self_echo(marker)

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


async def test_state_update_response_records_only_exact_nonterminal_evidence() -> None:
    recorder = AsyncMock()
    client = LinearClient(
        api_key="lin_api_test",  # pragma: allowlist secret
        session=AsyncMock(),
        self_echo_recorder=LinearSelfEchoRecorder(issue_state_updated=recorder),
    )

    await client.record_issue_state_update(
        _issue(),
        issue_id=_ISSUE_ID,
        state_id=_STATE_ID,
    )
    await client.record_issue_state_update(
        _issue(state_type="completed"),
        issue_id=_ISSUE_ID,
        state_id=_STATE_ID,
    )

    recorder.assert_awaited_once_with(
        {
            "id": _ISSUE_ID,
            "stateId": _STATE_ID,
            "updatedAt": _REVISION,
            "stateType": "started",
        }
    )


async def test_state_and_plan_mutations_forward_the_provider_receipt_to_the_recorder() -> None:
    recorder = AsyncMock()
    client = LinearClient(
        api_key="lin_api_test",  # pragma: allowlist secret
        session=AsyncMock(),
        self_echo_recorder=LinearSelfEchoRecorder(issue_state_updated=recorder),
    )
    client.query = AsyncMock(return_value={"issueUpdate": {"success": True, "issue": _issue()}})

    await update_issue_state(client, _ISSUE_ID, _STATE_ID)
    await update_issue_plan(
        client,
        issue_id=_ISSUE_ID,
        state_id=_STATE_ID,
        description="A plan.",
    )

    assert recorder.await_count == 2
    assert recorder.await_args.args[0]["stateId"] == _STATE_ID


async def test_query_only_client_can_update_state_and_plan_without_echo_recording() -> None:
    client = _QueryOnlyIssueClient()

    state_issue = await update_issue_state(client, _ISSUE_ID, _STATE_ID)
    plan_issue = await update_issue_plan(
        client,
        issue_id=_ISSUE_ID,
        state_id=_STATE_ID,
        description="A plan.",
    )

    assert state_issue["id"] == _ISSUE_ID
    assert plan_issue["id"] == _ISSUE_ID


async def test_host_client_context_records_the_state_marker_used_by_work_item_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    account = LinearAccount("linear-project", LinearTool(type="linear"))
    async with LinearClientContext(account) as client:
        client.query = AsyncMock(return_value={"issueUpdate": {"success": True, "issue": _issue()}})
        await update_issue_state(client, _ISSUE_ID, _STATE_ID)

    admitted = await admit_webhook_receipt(
        _receipt("delivery-host-context"),
        None,
        self_echo=_self_echo(),
    )

    assert admitted.self_echo_suppressed is True
