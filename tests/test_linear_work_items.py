"""Hermetic lifecycle tests for host-owned Linear work-item operations."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest

from pynchy.plugins.integrations.github_pull_requests import GitHubPullRequestRef
from pynchy.plugins.integrations.linear_boards import LinearWorkspaceBoard
from pynchy.plugins.integrations.linear_client import LinearClient
from pynchy.plugins.integrations.linear_work_item_actions import host_action_registration
from pynchy.plugins.integrations.linear_work_item_completion import (
    complete_merged_pull_request,
    complete_reviewed_work_item,
)
from pynchy.state import (
    begin_in_flight_turn,
    get_active_work_item_execution,
    init_test_database,
    list_work_item_executions,
    mark_work_item_delivery_delivered_for_turn,
)
from pynchy.types import InFlightTurn, InFlightWorkKind


@dataclass
class FakeLinearState:
    """In-memory Linear contract double, including a lost-response write outcome."""

    issue: dict[str, Any]
    fail_after_update: bool = False
    created_issue_count: int = 0

    async def get_issue(self, issue_id: str) -> dict[str, Any] | None:
        return deepcopy(self.issue) if issue_id == self.issue["id"] else None

    async def query(self, _query: str, **variables: object) -> dict[str, Any]:
        if "state_id" not in variables:
            raise AssertionError("test client only supports issue state updates")
        if "issueCreate" in _query:
            self.created_issue_count += 1
            self.issue = {
                "id": f"issue-{self.created_issue_count}",
                "identifier": f"PYN-{self.created_issue_count}",
                "title": variables["title"],
                "url": f"https://linear.app/example/issue/PYN-{self.created_issue_count}",
                "description": variables.get("description"),
                "updatedAt": "2026-07-18T17:00:00+00:00",
                "state": _state(str(variables["state_id"])),
                "project": {"id": "project-1", "name": "Pynchy"},
            }
            return {"issueCreate": {"success": True, "issue": deepcopy(self.issue)}}
        self.issue["state"] = _state(str(variables["state_id"]))
        if "description" in variables:
            self.issue["description"] = variables["description"]
        self.issue["updatedAt"] = "2026-07-18T17:00:00+00:00"
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
        "state-in-progress": {"id": "state-in-progress", "name": "In Progress", "type": "started"},
        "state-awaiting-review": {
            "id": "state-awaiting-review",
            "name": "Awaiting Review",
            "type": "started",
        },
        "state-blocked": {"id": "state-blocked", "name": "Blocked", "type": "started"},
        "state-done": {"id": "state-done", "name": "Done", "type": "completed"},
        "state-rejected": {"id": "state-rejected", "name": "Rejected", "type": "canceled"},
    }
    return states[state_id]


def _issue(*, state_id: str = "state-human-approved") -> dict[str, Any]:
    return {
        "id": "issue-1",
        "identifier": "PYN-1",
        "title": "Ship durable work item tracking",
        "url": "https://linear.app/example/issue/PYN-1",
        "description": "Acceptance criteria from the user.",
        "updatedAt": "2026-07-18T16:00:00+00:00",
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
            "blocked": _state("state-blocked"),
            "done": _state("state-done"),
            "rejected": _state("state-rejected"),
        },
    )


@pytest.fixture(autouse=True)
async def database() -> None:
    await init_test_database()


@pytest.fixture
def lifecycle(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeLinearState, dict[str, Any]]:
    state = FakeLinearState(_issue())
    client = LinearClient(api_key="lin_api_test", session=object())
    client.get_issue = state.get_issue  # type: ignore[method-assign]
    client.query = state.query  # type: ignore[method-assign]
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_items.linear_client",
        lambda **_kwargs: FakeLinearClientContext(client),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_completion.linear_client",
        lambda **_kwargs: FakeLinearClientContext(client),
    )

    board = AsyncMock(return_value=_board())
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_provider.require_workspace_board",
        board,
    )
    handlers = {action.tool_name: action.handler for action in host_action_registration().actions}
    return state, handlers


async def _call(
    handlers: dict[str, Any],
    tool: str,
    request_id: str,
    **arguments: object,
) -> dict[str, object]:
    return await handlers[tool]({"source_group": "pynchy", "request_id": request_id, **arguments})


async def _begin_direct_turn(
    content: str,
    *,
    group_folder: str = "pynchy",
    input_source: str = "user",
) -> None:
    await begin_in_flight_turn(
        InFlightTurn(
            turn_id="turn-request",
            chat_jid="pynchy@example.test",
            group_folder=group_folder,
            work_kind=InFlightWorkKind.INTERACTIVE,
            input_messages=[
                {
                    "message_type": "user",
                    "sender": "operator",
                    "sender_name": "Operator",
                    "content": content,
                }
            ],
            input_start_cursor="",
            input_end_cursor="",
            started_at="2026-07-18T17:00:00+00:00",
            input_source=input_source,
        )
    )


@pytest.mark.action("linear.todo.request")
async def test_direct_user_request_creates_ready_for_planning_item_in_parent_workspace(
    lifecycle,
    monkeypatch: pytest.MonkeyPatch,
):
    state, handlers = lifecycle
    source_group = "pynchy__thread_discord-channel-123"
    request = "Please implement durable Android automation for Wellhub."
    await _begin_direct_turn(request, group_folder=source_group)
    board = AsyncMock(return_value=_board())
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_boards.require_workspace_board",
        board,
    )

    result = await handlers["linear_create_requested_todo"](
        {
            "source_group": source_group,
            "request_id": "request-1",
            "title": "Create Wellhub Android automation",
            "description": "Use the existing Android MCP guidance.",
            "priority": 3,
            "authorization_quote": request,
        }
    )

    assert result["result"]["issue"]["state"]["name"] == "Ready for Planning"
    assert state.created_issue_count == 1
    assert state.issue["title"] == "Create Wellhub Android automation"
    workspace = board.await_args.args[1]
    assert workspace.folder == "pynchy"
    assert workspace.jid == "pynchy@example.test"


async def test_requested_todo_rejects_non_direct_turn(lifecycle):
    state, handlers = lifecycle
    request = "Create a follow-up issue for this finding."
    await _begin_direct_turn(request, input_source="scheduled_task")

    result = await _call(
        handlers,
        "linear_create_requested_todo",
        "request-1",
        title="Follow up on the finding",
        authorization_quote=request,
    )

    assert result == {
        "error": "A current direct user turn is required to create a Ready for Planning item"
    }
    assert state.created_issue_count == 0


async def test_requested_todo_rejects_partial_quote_from_current_user_message(lifecycle):
    state, handlers = lifecycle
    await _begin_direct_turn("Please implement an unrelated feature.")

    result = await _call(
        handlers,
        "linear_create_requested_todo",
        "request-1",
        title="Implement an unrelated feature",
        authorization_quote="implement an unrelated feature",
    )

    assert result == {
        "error": "authorization_quote must exactly quote a current direct user message"
    }
    assert state.created_issue_count == 0


@pytest.mark.action("linear.workitem.claim")
async def test_claim_persists_execution_before_moving_issue(lifecycle):
    client, handlers = lifecycle

    result = await _call(handlers, "linear_claim_work_item", "claim-1", issue_id="issue-1")

    execution = result["result"]["work_item"]
    assert execution["status"] == "in_progress"
    assert execution["attempt"] == 1
    assert execution["task_id"] is None
    assert client.issue["state"]["name"] == "In Progress"
    assert (await get_active_work_item_execution("issue-1")) is not None


async def test_claim_requires_explicit_human_approval(lifecycle):
    client, handlers = lifecycle
    client.issue["state"] = _state("state-awaiting-plan-approval")

    result = await _call(handlers, "linear_claim_work_item", "claim-1", issue_id="issue-1")

    assert result == {"error": "Linear work item must be Human Approved before Pynchy can claim it"}
    assert await get_active_work_item_execution("issue-1") is None


@pytest.mark.action("linear.workitem.review")
async def test_review_submission_links_pr_and_keeps_claim_until_merge(lifecycle):
    _client, handlers = lifecycle
    await _call(handlers, "linear_claim_work_item", "claim-1", issue_id="issue-1")

    result = await _call(
        handlers,
        "linear_await_review_work_item",
        "review-1",
        issue_id="issue-1",
        summary="Implemented and tested the lifecycle service.",
        pull_request_url="https://github.com/example/pynchy/pull/42",
        evidence_refs=["tests/test_linear_work_items.py"],
    )

    assert result["result"]["work_item"]["status"] == "awaiting_review"
    work_item = result["result"]["work_item"]
    assert work_item["summary"] == "Implemented and tested the lifecycle service."
    assert work_item["evidence_refs"] == [
        "https://github.com/example/pynchy/pull/42",
        "tests/test_linear_work_items.py",
    ]
    assert (await get_active_work_item_execution("issue-1")) is not None

    completed = await complete_merged_pull_request(
        "pynchy",
        GitHubPullRequestRef.parse("https://github.com/example/pynchy/pull/42"),
        "delivery-1",
    )

    assert completed is not None
    assert completed.status.value == "completed"
    assert await get_active_work_item_execution("issue-1") is None


async def test_linked_non_code_work_can_await_review_without_a_pull_request(lifecycle):
    _client, handlers = lifecycle
    await _call(handlers, "linear_claim_work_item", "claim-1", issue_id="issue-1")

    result = await _call(
        handlers,
        "linear_await_review_work_item",
        "review-1",
        issue_id="issue-1",
        summary="Purchased the approved equipment.",
        evidence_refs=["linear-attachment:receipt-1"],
    )

    work_item = result["result"]["work_item"]
    assert work_item["status"] == "awaiting_review"
    assert work_item["summary"] == "Purchased the approved equipment."
    assert work_item["evidence_refs"] == ["linear-attachment:receipt-1"]


async def test_existing_unlinked_work_moves_directly_to_awaiting_review(lifecycle):
    client, handlers = lifecycle
    client.issue["state"] = _state("state-awaiting-plan-approval")

    result = await _call(
        handlers,
        "linear_await_review_work_item",
        "review-1",
        issue_id="issue-1",
        summary="Verified the requested behavior already exists.",
        evidence_refs=["commit:abc123", "test:linear-webhooks"],
    )

    assert result["result"]["issue"]["state"]["name"] == "Awaiting Review"
    assert result["result"]["review"] == {
        "summary": "Verified the requested behavior already exists.",
        "evidence_refs": ["commit:abc123", "test:linear-webhooks"],
    }
    assert await get_active_work_item_execution("issue-1") is None


async def test_existing_terminal_work_cannot_reenter_review(lifecycle):
    client, handlers = lifecycle
    client.issue["state"] = _state("state-done")

    result = await _call(
        handlers,
        "linear_await_review_work_item",
        "review-1",
        issue_id="issue-1",
        summary="Do not reopen completed work.",
    )

    assert result == {"error": "A terminal Linear work item cannot re-enter Awaiting Review"}


async def test_linear_done_update_completes_linked_non_code_execution(lifecycle):
    client, handlers = lifecycle
    await _call(handlers, "linear_claim_work_item", "claim-1", issue_id="issue-1")
    await _call(
        handlers,
        "linear_await_review_work_item",
        "review-1",
        issue_id="issue-1",
        summary="Purchased the approved equipment.",
        evidence_refs=["linear-attachment:receipt-1"],
    )
    client.issue["state"] = _state("state-done")

    completed = await complete_reviewed_work_item("pynchy", "issue-1", "delivery-1")

    assert completed is not None
    assert completed.status.value == "completed"
    assert completed.evidence_refs == ("linear-attachment:receipt-1",)
    assert await get_active_work_item_execution("issue-1") is None


@pytest.mark.action("linear.workitem.block")
async def test_block_records_reason_and_keeps_claim(lifecycle):
    _client, handlers = lifecycle
    await _call(handlers, "linear_claim_work_item", "claim-1", issue_id="issue-1")

    result = await _call(
        handlers,
        "linear_block_work_item",
        "block-1",
        issue_id="issue-1",
        reason="Waiting for the owner to clarify the target workflow.",
    )

    assert result["result"]["work_item"]["status"] == "blocked"
    work_item = result["result"]["work_item"]
    assert work_item["blocker"] == "Waiting for the owner to clarify the target workflow."
    assert (await get_active_work_item_execution("issue-1")) is not None


@pytest.mark.action("linear.workitem.handoff")
async def test_handoff_sets_owner_and_releases_claim(lifecycle):
    _client, handlers = lifecycle
    await _call(handlers, "linear_claim_work_item", "claim-1", issue_id="issue-1")

    result = await _call(
        handlers,
        "linear_handoff_work_item",
        "handoff-1",
        issue_id="issue-1",
        owner="operator",
        summary="Needs an operator decision about production rollout.",
    )

    assert result["result"]["work_item"]["status"] == "handed_off"
    assert result["result"]["work_item"]["handoff_to"] == "operator"
    assert await get_active_work_item_execution("issue-1") is None


@pytest.mark.action("linear.workitem.reconcile")
async def test_reconcile_confirms_a_lost_provider_response(lifecycle):
    client, handlers = lifecycle
    client.fail_after_update = True

    claim = await _call(handlers, "linear_claim_work_item", "claim-1", issue_id="issue-1")
    assert claim["result"]["work_item"]["status"] == "unknown"
    client.fail_after_update = False

    result = await _call(
        handlers,
        "linear_reconcile_work_item",
        "reconcile-1",
        issue_id="issue-1",
    )

    assert result["result"]["work_item"]["status"] == "in_progress"


@pytest.mark.action("linear.todo.move")
async def test_generic_move_rejects_linked_items_and_moves_unlinked_items(lifecycle):
    client, handlers = lifecycle
    await _call(handlers, "linear_claim_work_item", "claim-1", issue_id="issue-1")

    linked = await _call(
        handlers,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status="done",
    )
    assert "lifecycle" in linked["error"]

    await _call(
        handlers,
        "linear_handoff_work_item",
        "handoff-1",
        issue_id="issue-1",
        owner="operator",
    )
    moved = await _call(
        handlers,
        "linear_move_todo",
        "move-2",
        issue_id="issue-1",
        status="agent_proposed",
    )
    assert moved["result"]["issue"]["state"]["name"] == "Agent Proposed"
    assert client.issue["state"]["name"] == "Agent Proposed"


@pytest.mark.parametrize("status", ["awaiting_plan_approval", "human_approved"])
async def test_generic_move_cannot_bypass_plan_or_human_approval(lifecycle, status):
    _client, handlers = lifecycle

    result = await _call(
        handlers,
        "linear_move_todo",
        "move-1",
        issue_id="issue-1",
        status=status,
    )

    assert result == {"error": "Agents may move unlinked Linear items only to: agent_proposed"}


@pytest.mark.action("linear.todo.plan")
async def test_submit_plan_persists_markdown_and_advances_only_to_plan_approval(lifecycle):
    client, handlers = lifecycle
    client.issue["state"] = _state("state-ready-for-planning")

    result = await _call(
        handlers,
        "linear_submit_plan",
        "plan-1",
        issue_id="issue-1",
        plan="1. Add the failing test.\n2. Implement the behavior.\n3. Run the full gate.",
    )

    assert result["result"]["issue"]["state"]["name"] == "Awaiting Plan Approval"
    assert client.issue["description"].startswith("Acceptance criteria from the user.")
    assert "<!-- pynchy.plan:start -->" in client.issue["description"]
    assert "1. Add the failing test." in client.issue["description"]
    assert "<!-- pynchy.plan:end -->" in client.issue["description"]


async def test_submit_plan_requires_ready_for_planning_state(lifecycle):
    _client, handlers = lifecycle

    result = await _call(
        handlers,
        "linear_submit_plan",
        "plan-1",
        issue_id="issue-1",
        plan="A concrete plan.",
    )

    assert result == {"error": "Linear work item must be Ready for Planning before planning"}


async def test_submit_plan_from_dynamic_thread_uses_parent_workspace_board(
    lifecycle,
    monkeypatch: pytest.MonkeyPatch,
):
    client, handlers = lifecycle
    client.issue["state"] = _state("state-ready-for-planning")
    board = AsyncMock(return_value=_board())
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_provider.require_workspace_board",
        board,
    )

    result = await handlers["linear_submit_plan"](
        {
            "source_group": "pynchy__thread_discord-channel-123",
            "request_id": "plan-1",
            "issue_id": "issue-1",
            "plan": "A concrete plan.",
        }
    )

    assert "error" not in result
    assert board.await_args.args[1].folder == "pynchy"


@pytest.mark.action("linear.workitem.list")
async def test_list_returns_bounded_operator_projection(lifecycle):
    _client, handlers = lifecycle
    await _call(handlers, "linear_claim_work_item", "claim-1", issue_id="issue-1")

    result = await _call(handlers, "linear_list_work_items", "list-1")

    assert result["result"]["work_items"][0]["issue"]["identifier"] == "PYN-1"
    assert len(await list_work_item_executions(workspace="pynchy")) == 1


async def test_resumed_turn_reuses_its_existing_claim(lifecycle):
    _client, handlers = lifecycle
    await begin_in_flight_turn(
        InFlightTurn(
            turn_id="turn-1",
            chat_jid="pynchy@example.test",
            group_folder="pynchy",
            work_kind=InFlightWorkKind.SCHEDULED,
            input_messages=[],
            input_start_cursor="",
            input_end_cursor="",
            started_at="2026-07-18T17:00:00+00:00",
            task_id="task-1",
        )
    )

    first = await _call(handlers, "linear_claim_work_item", "claim-1", issue_id="issue-1")
    resumed = await _call(handlers, "linear_claim_work_item", "claim-2", issue_id="issue-1")

    assert (
        resumed["result"]["work_item"]["execution_id"]
        == first["result"]["work_item"]["execution_id"]
    )
    assert len(await list_work_item_executions(workspace="pynchy")) == 1


async def test_second_turn_cannot_claim_an_active_issue(lifecycle):
    _client, handlers = lifecycle
    await _call(handlers, "linear_claim_work_item", "claim-1", issue_id="issue-1")

    duplicate = await _call(handlers, "linear_claim_work_item", "claim-2", issue_id="issue-1")

    assert duplicate["error"] == "Linear work item is already claimed"
    assert len(await list_work_item_executions(workspace="pynchy")) == 1


async def test_workspace_cannot_read_another_workspace_claim(lifecycle):
    _client, handlers = lifecycle
    await _call(handlers, "linear_claim_work_item", "claim-1", issue_id="issue-1")

    result = await _call(
        handlers,
        "linear_claim_work_item",
        "claim-2",
        issue_id="issue-1",
        source_group="other-workspace",
    )

    assert result == {"error": "Linear work item is already claimed"}


async def test_claim_rejects_an_issue_from_another_workspace_board(lifecycle):
    client, handlers = lifecycle
    client.issue["project"] = {"id": "project-other", "name": "Other"}

    result = await _call(handlers, "linear_claim_work_item", "claim-1", issue_id="issue-1")

    assert result["error"] == "Linear issue does not belong to this Pynchy workspace board"
    assert await get_active_work_item_execution("issue-1") is None


async def test_remote_state_conflict_stays_visible_for_reconciliation(lifecycle):
    client, handlers = lifecycle
    await _call(handlers, "linear_claim_work_item", "claim-1", issue_id="issue-1")
    client.issue["state"] = _state("state-human-approved")

    result = await _call(
        handlers,
        "linear_await_review_work_item",
        "review-1",
        issue_id="issue-1",
        summary="Completed locally, but the board moved first.",
        pull_request_url="https://github.com/example/pynchy/pull/42",
    )

    assert "conflicted" in result["error"]
    assert result["result"]["work_item"]["status"] == "failed"


async def test_requester_delivery_remains_separate_until_final_result(lifecycle):
    _client, handlers = lifecycle
    await begin_in_flight_turn(
        InFlightTurn(
            turn_id="turn-1",
            chat_jid="pynchy@example.test",
            group_folder="pynchy",
            work_kind=InFlightWorkKind.INTERACTIVE,
            input_messages=[],
            input_start_cursor="",
            input_end_cursor="",
            started_at="2026-07-18T17:00:00+00:00",
        )
    )
    await _call(handlers, "linear_claim_work_item", "claim-1", issue_id="issue-1")

    review_ready = await _call(
        handlers,
        "linear_await_review_work_item",
        "review-1",
        issue_id="issue-1",
        summary="Ready for review.",
        pull_request_url="https://github.com/example/pynchy/pull/42",
    )
    assert review_ready["result"]["work_item"]["requester_delivery"]["status"] == "pending"

    await mark_work_item_delivery_delivered_for_turn("turn-1")

    execution = (await list_work_item_executions(workspace="pynchy"))[0]
    assert execution.requester_delivery_status == "delivered"
    assert execution.requester_delivered_at is not None

    completed = await complete_merged_pull_request(
        "pynchy",
        GitHubPullRequestRef.parse("https://github.com/example/pynchy/pull/42"),
        "delivery-1",
    )
    assert completed is not None
    assert completed.requester_delivery_status == "delivered"
    assert completed.requester_delivered_at == execution.requester_delivered_at
