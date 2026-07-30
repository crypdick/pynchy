"""Durable correlation for Pynchy-created Linear issue-state updates."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pynchy.plugins.integrations.linear_client import (
    LinearClient,
    LinearError,
    LinearSelfEchoRecorder,
    normalize_comment_create_response,
    normalize_issue_state_update_response,
)
from pynchy.plugins.integrations.linear_plans import update_issue_plan
from pynchy.plugins.integrations.linear_work_item_provider import update_issue_state
from pynchy.state import init_test_database
from pynchy.webhook_effects import (
    WebhookEffectEvidence,
    WebhookEffectId,
    WebhookEffectScope,
)

_REVISION = "2026-07-26T16:00:01+00:00"
_ISSUE_ID = "issue-1"
_STATE_ID = "state-awaiting-review"


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


def _issue() -> dict[str, object]:
    return {
        "id": _ISSUE_ID,
        "updatedAt": _REVISION,
        "state": {"id": _STATE_ID, "type": "started"},
    }


class _QueryOnlyIssueClient:
    async def query(self, _query: str, **_variables: object) -> dict[str, object]:
        return {"issueUpdate": {"success": True, "issue": _issue()}}


class _ConstructorlessLinearClient(LinearClient):
    """Query fake that deliberately omits the network client's constructor."""

    def __init__(self) -> None:
        pass

    async def query(self, _query: str, **_variables: object) -> dict[str, object]:
        return {"issueUpdate": {"success": True, "issue": _issue()}}


def _recorder(calls: list[str]) -> LinearSelfEchoRecorder:
    async def begin(  # noqa: RUF029 - async recorder callback contract.
        scope: WebhookEffectScope,
    ) -> WebhookEffectId:
        assert scope.subject_id == _ISSUE_ID
        calls.append("begin")
        return WebhookEffectId(f"effect-{len(calls)}")

    async def executing(  # noqa: RUF029 - async recorder callback contract.
        _effect_id: WebhookEffectId,
    ) -> None:
        calls.append("executing")

    async def confirm(  # noqa: RUF029 - async recorder callback contract.
        _effect_id: WebhookEffectId,
        evidence: WebhookEffectEvidence,
    ) -> None:
        assert evidence.scope.subject_id == _ISSUE_ID
        calls.append("confirm")

    return LinearSelfEchoRecorder(
        account_name="linear-project",
        begin=begin,
        mark_executing=executing,
        confirm=confirm,
        fail=AsyncMock(),
        mark_outcome_unknown=AsyncMock(),
    )


async def test_state_and_plan_mutations_begin_before_query_and_confirm_after_response() -> None:
    calls: list[str] = []
    client = LinearClient(
        api_key="lin_api_test",  # pragma: allowlist secret
        session=AsyncMock(),
        self_echo_recorder=_recorder(calls),
    )

    async def query(  # noqa: RUF029 - async provider client contract.
        _query: str,
        **_variables: object,
    ):
        calls.append("query")
        return {"issueUpdate": {"success": True, "issue": _issue()}}

    client.query = query

    await update_issue_state(client, _ISSUE_ID, _STATE_ID)
    await update_issue_plan(
        client,
        issue_id=_ISSUE_ID,
        state_id=_STATE_ID,
        description="A plan.",
    )

    assert calls == [
        "begin",
        "executing",
        "query",
        "confirm",
        "begin",
        "executing",
        "query",
        "confirm",
    ]


async def test_query_only_client_keeps_mutation_helpers_provider_agnostic() -> None:
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


async def test_constructorless_linear_query_fake_does_not_require_a_recorder() -> None:
    issue = await update_issue_state(
        _ConstructorlessLinearClient(),
        _ISSUE_ID,
        _STATE_ID,
    )

    assert issue["id"] == _ISSUE_ID


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({"issueUpdate": {"success": False}}, "did not update"),
        ({"issueUpdate": {"success": True}}, "did not include an issue"),
    ],
)
async def test_query_only_client_rejects_incomplete_issue_update_responses(
    response: dict[str, object],
    message: str,
) -> None:
    client = _ConstructorlessLinearClient()
    client.query = AsyncMock(return_value=response)

    with pytest.raises(LinearError, match=message):
        await update_issue_state(client, _ISSUE_ID, _STATE_ID)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({"issueUpdate": {"success": False}}, "did not persist"),
        ({"issueUpdate": {"success": True}}, "plan response did not include"),
    ],
)
async def test_query_only_client_rejects_incomplete_plan_update_responses(
    response: dict[str, object],
    message: str,
) -> None:
    client = _ConstructorlessLinearClient()
    client.query = AsyncMock(return_value=response)

    with pytest.raises(LinearError, match=message):
        await update_issue_plan(
            client,
            issue_id=_ISSUE_ID,
            state_id=_STATE_ID,
            description="A plan.",
        )


async def test_provider_declared_state_failure_releases_effect_instead_of_quarantining() -> None:
    recorder = LinearSelfEchoRecorder(
        account_name="linear-project",
        begin=AsyncMock(return_value=WebhookEffectId("effect-1")),
        mark_executing=AsyncMock(),
        confirm=AsyncMock(),
        fail=AsyncMock(),
        mark_outcome_unknown=AsyncMock(),
    )
    client = LinearClient(
        api_key="lin_api_test",  # pragma: allowlist secret
        session=AsyncMock(),
        self_echo_recorder=recorder,
    )
    client.query = AsyncMock(return_value={"issueUpdate": {"success": False}})

    with pytest.raises(LinearError, match="did not update"):
        await update_issue_state(client, _ISSUE_ID, _STATE_ID)

    recorder.fail.assert_awaited_once_with(WebhookEffectId("effect-1"))
    recorder.mark_outcome_unknown.assert_not_awaited()


@pytest.mark.parametrize(
    ("comment", "message"),
    [
        ({"id": "comment", "issueId": _ISSUE_ID, "createdAt": _REVISION}, "lacks"),
        (
            {
                "id": "comment",
                "issueId": "other-issue",
                "createdAt": _REVISION,
                "updatedAt": _REVISION,
            },
            "another issue",
        ),
    ],
)
def test_comment_response_requires_matching_write_evidence(
    comment: dict[str, object], message: str
) -> None:
    with pytest.raises(LinearError, match=message):
        normalize_comment_create_response(comment, _ISSUE_ID)


@pytest.mark.parametrize(
    ("issue", "message"),
    [
        ({}, "lacks"),
        (
            {"id": "other-issue", "updatedAt": _REVISION, "state": {"id": _STATE_ID}},
            "another issue",
        ),
        (
            {"id": _ISSUE_ID, "updatedAt": _REVISION, "state": {"id": "other-state"}},
            "another state",
        ),
    ],
)
def test_issue_state_response_requires_matching_write_evidence(
    issue: dict[str, object], message: str
) -> None:
    with pytest.raises(LinearError, match=message):
        normalize_issue_state_update_response(
            issue,
            issue_id=_ISSUE_ID,
            state_id=_STATE_ID,
        )
