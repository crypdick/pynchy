"""Hermetic tests for Linear authority, leasing, and generic agent actions."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest

from pynchy.plugins.integrations.linear_boards import LinearWorkspaceBoard
from pynchy.plugins.integrations.linear_client import LinearClient
from pynchy.plugins.integrations.linear_work_item_actions import host_action_registration
from pynchy.plugins.integrations.linear_work_item_completion import (
    complete_reviewed_work_item,
)
from pynchy.plugins.integrations.linear_work_item_provider import (
    WorkItemLeaseRequest,
    acquire_human_started_work_item_lease,
    acquire_work_item_lease,
)
from pynchy.state import (
    WorkItemClaimConflictError,
    begin_in_flight_turn,
    clear_in_flight_turn,
    get_active_work_item_execution,
    init_test_database,
    list_work_item_executions,
)
from pynchy.types import InFlightTurn, InFlightWorkKind


@dataclass
class FakeLinearState:
    """In-memory Linear boundary, including an accepted write with a lost response."""

    issue: dict[str, Any]
    fail_after_update: bool = False

    async def get_issue(self, issue_id: str) -> dict[str, Any] | None:
        return deepcopy(self.issue) if issue_id == self.issue["id"] else None

    async def query(self, _query: str, **variables: object) -> dict[str, Any]:
        state_id = variables.get("state_id")
        if not isinstance(state_id, str):
            raise TypeError("test client only supports issue state updates")
        description = variables.get("description")
        if description is not None:
            if not isinstance(description, str):
                raise TypeError("test plan description must be text")
            self.issue["description"] = description
        self.issue["state"] = _state(state_id)
        self.issue["updatedAt"] = "2026-07-25T17:00:00+00:00"
        if self.fail_after_update:
            raise RuntimeError("connection closed after provider accepted mutation")
        return {"issueUpdate": {"success": True, "issue": deepcopy(self.issue)}}


class FakeLinearClientContext:
    def __init__(self, client: LinearClient) -> None:
        self._client = client

    async def __aenter__(self) -> LinearClient:
        return self._client

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        return None


@dataclass(frozen=True)
class Lifecycle:
    state: FakeLinearState
    client: LinearClient
    handlers: dict[str, Any]


def _state(state_id: str) -> dict[str, str]:
    states = {
        "state-agent-proposed": {
            "id": "state-agent-proposed",
            "name": "Agent Proposed",
            "type": "backlog",
        },
        "state-ready-for-planning": {
            "id": "state-ready-for-planning",
            "name": "Ready for Planning",
            "type": "unstarted",
        },
        "state-awaiting-plan-approval": {
            "id": "state-awaiting-plan-approval",
            "name": "Awaiting Plan Approval",
            "type": "unstarted",
        },
        "state-human-approved": {
            "id": "state-human-approved",
            "name": "Human Approved",
            "type": "unstarted",
        },
        "state-in-progress": {
            "id": "state-in-progress",
            "name": "In Progress",
            "type": "started",
        },
        "state-awaiting-review": {
            "id": "state-awaiting-review",
            "name": "Awaiting Review",
            "type": "started",
        },
        "state-follow-ups": {
            "id": "state-follow-ups",
            "name": "Follow-ups",
            "type": "started",
        },
        "state-blocked": {
            "id": "state-blocked",
            "name": "Blocked",
            "type": "started",
        },
        "state-done": {"id": "state-done", "name": "Done", "type": "completed"},
        "state-rejected": {
            "id": "state-rejected",
            "name": "Rejected",
            "type": "canceled",
        },
    }
    return states[state_id]


def _issue(*, state_id: str = "state-human-approved") -> dict[str, Any]:
    return {
        "id": "issue-1",
        "identifier": "PYN-1",
        "title": "Let agents manage the work",
        "url": "https://linear.app/example/issue/PYN-1",
        "updatedAt": "2026-07-25T16:00:00+00:00",
        "state": _state(state_id),
        "project": {"id": "project-1", "name": "Pynchy"},
    }


def _board() -> LinearWorkspaceBoard:
    return LinearWorkspaceBoard(
        team={"id": "team-1"},
        project={"id": "project-1", "name": "Pynchy"},
        states={
            "agent_proposed": _state("state-agent-proposed"),
            "ready_for_planning": _state("state-ready-for-planning"),
            "awaiting_plan_approval": _state("state-awaiting-plan-approval"),
            "human_approved": _state("state-human-approved"),
            "in_progress": _state("state-in-progress"),
            "awaiting_review": _state("state-awaiting-review"),
            "follow_ups": _state("state-follow-ups"),
            "blocked": _state("state-blocked"),
            "done": _state("state-done"),
            "rejected": _state("state-rejected"),
        },
    )


@pytest.fixture(autouse=True)
async def database() -> None:
    await init_test_database()


@pytest.fixture
def lifecycle(monkeypatch: pytest.MonkeyPatch) -> Lifecycle:
    state = FakeLinearState(_issue())
    client = LinearClient(api_key="lin_api_test", session=object())
    client.get_issue = state.get_issue  # type: ignore[method-assign]
    client.query = state.query  # type: ignore[method-assign]
    context = lambda **_kwargs: FakeLinearClientContext(client)  # noqa: E731
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_items.linear_client",
        context,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_completion.linear_client",
        context,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_provider.require_workspace_board",
        AsyncMock(return_value=_board()),
    )
    handlers = {action.tool_name: action.handler for action in host_action_registration().actions}
    return Lifecycle(state=state, client=client, handlers=handlers)


async def _call(
    lifecycle: Lifecycle,
    tool: str,
    request_id: str,
    *,
    source_group: str = "pynchy",
    **arguments: object,
) -> dict[str, object]:
    return await lifecycle.handlers[tool](
        {
            "source_group": source_group,
            "request_id": request_id,
            **arguments,
        }
    )


async def _lease(
    lifecycle: Lifecycle,
    request_id: str = "lease-1",
    *,
    workspace: str = "pynchy",
):
    return await acquire_work_item_lease(
        lifecycle.client,
        WorkItemLeaseRequest(
            workspace=workspace,
            issue_id="issue-1",
            request_id=request_id,
            initiated_by="linear-webhook:test",
        ),
    )


async def _begin_turn(*, input_source: str = "user") -> None:
    await begin_in_flight_turn(
        InFlightTurn(
            turn_id="turn-1",
            chat_jid="pynchy@example.test",
            group_folder="pynchy",
            work_kind=InFlightWorkKind.INTERACTIVE,
            input_messages=[],
            input_start_cursor="",
            input_end_cursor="",
            started_at="2026-07-25T17:00:00+00:00",
            input_source=input_source,
        )
    )


@pytest.mark.action("linear.todo.plan")
async def test_submit_plan_persists_markdown_and_waits_for_human_approval(
    lifecycle: Lifecycle,
) -> None:
    lifecycle.state.issue["state"] = _state("state-ready-for-planning")
    lifecycle.state.issue["description"] = "Acceptance criteria from the user."

    result = await _call(
        lifecycle,
        "linear_submit_plan",
        "plan-1",
        issue_id="issue-1",
        plan="1. Add the failing test.\n2. Implement the behavior.\n3. Run the full gate.",
    )

    issue = result["result"]["issue"]
    assert issue["state"]["name"] == "Awaiting Plan Approval"
    assert lifecycle.state.issue["description"].startswith("Acceptance criteria from the user.")
    assert "<!-- pynchy.plan:start -->" in lifecycle.state.issue["description"]
    assert "1. Add the failing test." in lifecycle.state.issue["description"]
    assert "<!-- pynchy.plan:end -->" in lifecycle.state.issue["description"]
    assert await get_active_work_item_execution("issue-1") is None


async def test_submit_plan_requires_ready_for_planning(
    lifecycle: Lifecycle,
) -> None:
    result = await _call(
        lifecycle,
        "linear_submit_plan",
        "plan-1",
        issue_id="issue-1",
        plan="A concrete plan.",
    )

    assert result == {"error": "Linear work item must be Ready for Planning before planning"}


async def test_host_lease_persists_before_moving_to_in_progress(
    lifecycle: Lifecycle,
) -> None:
    execution = await _lease(lifecycle)

    assert execution.status.value == "in_progress"
    assert execution.attempt == 1
    assert lifecycle.state.issue["state"]["name"] == "In Progress"
    assert await get_active_work_item_execution("issue-1") is not None


async def test_host_lease_requires_human_approved(lifecycle: Lifecycle) -> None:
    lifecycle.state.issue["state"] = _state("state-agent-proposed")

    with pytest.raises(ValueError, match="must be Human Approved"):
        await _lease(lifecycle)

    assert await get_active_work_item_execution("issue-1") is None


async def test_human_started_lease_adopts_in_progress_without_provider_move(
    lifecycle: Lifecycle,
) -> None:
    lifecycle.state.issue["state"] = _state("state-in-progress")

    execution = await acquire_human_started_work_item_lease(
        lifecycle.client,
        WorkItemLeaseRequest(
            workspace="pynchy",
            issue_id="issue-1",
            request_id="human-started-1",
            initiated_by="linear-webhook:delivery-1:user:user-1",
        ),
    )

    assert execution.status.value == "in_progress"
    assert lifecycle.state.issue["state"]["name"] == "In Progress"
    assert await get_active_work_item_execution("issue-1") is not None


async def test_human_started_lease_requires_current_in_progress_state(
    lifecycle: Lifecycle,
) -> None:
    with pytest.raises(ValueError, match="must be human-started"):
        await acquire_human_started_work_item_lease(
            lifecycle.client,
            WorkItemLeaseRequest(
                workspace="pynchy",
                issue_id="issue-1",
                request_id="human-started-1",
                initiated_by="linear-webhook:delivery-1:user:user-1",
            ),
        )

    assert await get_active_work_item_execution("issue-1") is None


async def test_lease_retry_reconciles_a_lost_provider_response(
    lifecycle: Lifecycle,
) -> None:
    lifecycle.state.fail_after_update = True
    uncertain = await _lease(lifecycle)
    lifecycle.state.fail_after_update = False

    resumed = await _lease(lifecycle)

    assert uncertain.status.value == "unknown"
    assert resumed.status.value == "in_progress"
    assert resumed.id == uncertain.id


async def test_second_trigger_cannot_take_an_active_lease(lifecycle: Lifecycle) -> None:
    await _lease(lifecycle)

    with pytest.raises(WorkItemClaimConflictError):
        await _lease(lifecycle, "lease-2")


@pytest.mark.action("linear.todo.move")
async def test_agent_can_move_owned_work_to_awaiting_review(
    lifecycle: Lifecycle,
) -> None:
    await _lease(lifecycle)
    await _begin_turn(input_source="scheduled_task")

    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status="awaiting_review",
    )

    work_item = result["result"]["work_item"]
    assert work_item["status"] == "awaiting_review"
    assert work_item["turn_id"] == "turn-1"
    assert await get_active_work_item_execution("issue-1") is None


async def test_blocked_work_can_be_reauthorized_for_a_new_attempt(
    lifecycle: Lifecycle,
) -> None:
    await _lease(lifecycle)
    blocked = await _call(
        lifecycle,
        "linear_move_todo",
        "move-blocked",
        issue_id="issue-1",
        status="blocked",
    )
    await _begin_turn()
    approved = await _call(
        lifecycle,
        "linear_move_todo",
        "move-approved",
        issue_id="issue-1",
        status="human_approved",
    )
    retried = await _lease(lifecycle, "lease-2")

    assert blocked["result"]["work_item"]["status"] == "blocked"
    assert approved["result"]["issue"]["state"]["name"] == "Human Approved"
    assert retried.attempt == 2


@pytest.mark.parametrize("status", ["human_approved", "rejected"])
async def test_agent_cannot_assert_human_decisions(
    lifecycle: Lifecycle,
    status: str,
) -> None:
    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status=status,
    )

    assert "direct-human instruction" in result["error"]


@pytest.mark.parametrize("status", ["human_approved", "rejected"])
async def test_direct_human_can_move_unlinked_work(
    lifecycle: Lifecycle,
    status: str,
) -> None:
    lifecycle.state.issue["state"] = _state("state-agent-proposed")
    await _begin_turn()

    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status=status,
    )

    assert result["result"]["issue"]["state"]["name"] == _board().states[status]["name"]


async def test_mark_done_needs_no_authorization_quote(lifecycle: Lifecycle) -> None:
    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status="done",
    )

    assert result["result"]["issue"]["state"]["name"] == "Done"


async def test_agent_can_finish_follow_ups_in_a_later_turn(lifecycle: Lifecycle) -> None:
    await _lease(lifecycle)
    await _begin_turn(input_source="scheduled_task")
    awaiting = await _call(
        lifecycle,
        "linear_move_todo",
        "move-awaiting",
        issue_id="issue-1",
        status="awaiting_review",
    )
    await clear_in_flight_turn("turn-1")
    await _begin_turn(input_source="trusted:linear:follow-ups")

    follow_ups = await _call(
        lifecycle,
        "linear_move_todo",
        "move-follow-ups",
        issue_id="issue-1",
        status="follow_ups",
    )
    done = await _call(
        lifecycle,
        "linear_move_todo",
        "move-done",
        issue_id="issue-1",
        status="done",
    )

    assert awaiting["result"]["work_item"]["turn_id"] == "turn-1"
    assert follow_ups["result"]["work_item"]["status"] == "follow_ups"
    assert done["result"]["work_item"]["status"] == "completed"


async def test_only_direct_human_can_reopen_terminal_work(
    lifecycle: Lifecycle,
) -> None:
    lifecycle.state.issue["state"] = _state("state-done")
    denied = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status="agent_proposed",
    )
    await _begin_turn()
    reopened = await _call(
        lifecycle,
        "linear_move_todo",
        "move-2",
        issue_id="issue-1",
        status="agent_proposed",
    )

    assert "direct-human instruction" in denied["error"]
    assert reopened["result"]["issue"]["state"]["name"] == "Agent Proposed"


async def test_in_progress_is_always_host_managed(lifecycle: Lifecycle) -> None:
    await _begin_turn()

    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status="in_progress",
    )

    assert result == {"error": "In Progress is managed by the host execution lease"}


async def test_active_execution_rejects_nonterminal_agent_move(
    lifecycle: Lifecycle,
) -> None:
    await _lease(lifecycle)

    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status="agent_proposed",
    )

    assert "active execution must move" in result["error"]


async def test_direct_human_done_completes_active_execution(
    lifecycle: Lifecycle,
) -> None:
    await _lease(lifecycle)
    await _begin_turn()

    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status="done",
    )

    assert result["result"]["work_item"]["status"] == "completed"
    assert await get_active_work_item_execution("issue-1") is None


async def test_done_webhook_completes_execution_still_in_progress(
    lifecycle: Lifecycle,
) -> None:
    await _lease(lifecycle)
    lifecycle.state.issue["state"] = _state("state-done")

    completed = await complete_reviewed_work_item("pynchy", "issue-1", "delivery-1")

    assert completed is not None
    assert completed.status.value == "completed"


@pytest.mark.action("linear.workitem.reconcile")
async def test_reconcile_confirms_lost_move_response(lifecycle: Lifecycle) -> None:
    await _lease(lifecycle)
    lifecycle.state.fail_after_update = True
    uncertain = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status="awaiting_review",
    )
    lifecycle.state.fail_after_update = False

    result = await _call(
        lifecycle,
        "linear_reconcile_work_item",
        "reconcile-1",
        issue_id="issue-1",
    )

    assert uncertain["result"]["work_item"]["status"] == "unknown"
    assert result["result"]["work_item"]["status"] == "awaiting_review"


async def test_remote_state_conflict_is_durable(lifecycle: Lifecycle) -> None:
    await _lease(lifecycle)
    lifecycle.state.issue["state"] = _state("state-human-approved")

    result = await _call(
        lifecycle,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status="awaiting_review",
    )

    assert "conflicted" in result["error"]
    assert result["result"]["work_item"]["status"] == "failed"


@pytest.mark.action("linear.workitem.list")
async def test_list_returns_workspace_execution_projection(
    lifecycle: Lifecycle,
) -> None:
    await _lease(lifecycle)

    result = await _call(lifecycle, "linear_list_work_items", "list-1")

    assert result["result"]["work_items"][0]["issue"]["identifier"] == "PYN-1"
    assert len(await list_work_item_executions(workspace="pynchy")) == 1


async def test_lease_rejects_issue_from_another_workspace_board(
    lifecycle: Lifecycle,
) -> None:
    lifecycle.state.issue["project"] = {"id": "project-other", "name": "Other"}

    with pytest.raises(ValueError, match="does not belong"):
        await _lease(lifecycle)
