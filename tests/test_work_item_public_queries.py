"""Public durable work-item query and input-boundary behavior."""

from __future__ import annotations

import pytest

from pynchy.state import (
    WorkItemClaimRequest,
    create_work_item_claim,
    list_work_item_executions,
)

pytest_plugins = ("tests.state_support",)


def _issue(issue_id: str) -> dict[str, object]:
    return {
        "id": issue_id,
        "identifier": f"SYN-{issue_id}",
        "url": f"https://linear.app/example/issue/{issue_id}",
        "state": {"id": "approved", "name": "Human Approved"},
    }


def _claim(*, workspace: str, issue: dict[str, object]) -> WorkItemClaimRequest:
    return WorkItemClaimRequest(
        workspace=workspace,
        issue=issue,
        turn_id=None,
        task_id=None,
        initiated_by="test",
        request_id=f"claim-{issue.get('id', 'invalid')}",
    )


async def test_work_item_listing_scopes_workspace_without_hiding_other_workspaces() -> None:
    alpha = await create_work_item_claim(_claim(workspace="alpha", issue=_issue("1")))
    beta = await create_work_item_claim(_claim(workspace="beta", issue=_issue("2")))

    assert [execution.id for execution in await list_work_item_executions(workspace="alpha")] == [
        alpha.id
    ]
    assert {execution.id for execution in await list_work_item_executions()} == {alpha.id, beta.id}


async def test_work_item_listing_can_return_every_execution_for_reconciliation() -> None:
    for index in range(101):
        await create_work_item_claim(_claim(workspace="alpha", issue=_issue(f"all-{index}")))

    assert len(await list_work_item_executions()) == 100
    assert len(await list_work_item_executions(limit=None)) == 101


@pytest.mark.parametrize(
    ("issue", "error", "message"),
    [
        ({"state": {"id": "approved", "name": "Human Approved"}}, ValueError, "missing id"),
        (
            {
                "id": "3",
                "identifier": "SYN-3",
                "url": "https://linear.app/example/issue/3",
                "state": "approved",
            },
            TypeError,
            "missing state",
        ),
    ],
)
async def test_work_item_claim_rejects_incomplete_external_issue(
    issue: dict[str, object],
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        await create_work_item_claim(_claim(workspace="alpha", issue=issue))
