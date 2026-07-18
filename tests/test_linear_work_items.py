"""Hermetic lifecycle tests for host-owned Linear work-item operations."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest

from pynchy.plugins.integrations.linear_boards import LinearWorkspaceBoard
from pynchy.plugins.integrations.linear_client import LinearClient
from pynchy.plugins.integrations.linear_work_item_actions import host_action_registration
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

    async def get_issue(self, issue_id: str) -> dict[str, Any] | None:
        return deepcopy(self.issue) if issue_id == self.issue["id"] else None

    async def query(self, _query: str, **variables: object) -> dict[str, Any]:
        if "state_id" not in variables:
            raise AssertionError("test client only supports issue state updates")
        self.issue["state"] = _state(str(variables["state_id"]))
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
        "state-ready": {"id": "state-ready", "name": "Ready", "type": "unstarted"},
        "state-in-progress": {"id": "state-in-progress", "name": "In Progress", "type": "started"},
        "state-blocked": {"id": "state-blocked", "name": "Blocked", "type": "started"},
        "state-done": {"id": "state-done", "name": "Done", "type": "completed"},
        "state-backlog": {"id": "state-backlog", "name": "Backlog", "type": "backlog"},
    }
    return states[state_id]


def _issue(*, state_id: str = "state-ready") -> dict[str, Any]:
    return {
        "id": "issue-1",
        "identifier": "PYN-1",
        "title": "Ship durable work item tracking",
        "url": "https://linear.app/example/issue/PYN-1",
        "updatedAt": "2026-07-18T16:00:00+00:00",
        "state": _state(state_id),
        "project": {"id": "project-1", "name": "Pynchy"},
    }


def _board() -> LinearWorkspaceBoard:
    return LinearWorkspaceBoard(
        team={"id": "team-1"},
        project={"id": "project-1", "name": "Pynchy"},
        states={
            "backlog": _state("state-backlog"),
            "planning": {"id": "state-planning", "name": "Planning", "type": "unstarted"},
            "ready": _state("state-ready"),
            "in_progress": _state("state-in-progress"),
            "blocked": _state("state-blocked"),
            "done": _state("state-done"),
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
        lambda: FakeLinearClientContext(client),
    )

    board = AsyncMock(return_value=_board())
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_provider.ensure_workspace_board",
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


@pytest.mark.action("linear.workitem.complete")
async def test_complete_records_summary_and_releases_claim(lifecycle):
    _client, handlers = lifecycle
    await _call(handlers, "linear_claim_work_item", "claim-1", issue_id="issue-1")

    result = await _call(
        handlers,
        "linear_complete_work_item",
        "complete-1",
        issue_id="issue-1",
        summary="Implemented and tested the lifecycle service.",
        evidence_refs=["tests/test_linear_work_items.py"],
    )

    assert result["result"]["work_item"]["status"] == "completed"
    work_item = result["result"]["work_item"]
    assert work_item["summary"] == "Implemented and tested the lifecycle service."
    assert work_item["evidence_refs"] == ["tests/test_linear_work_items.py"]
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
        status="backlog",
    )
    assert moved["result"]["issue"]["state"]["name"] == "Backlog"
    assert client.issue["state"]["name"] == "Backlog"


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
    client.issue["state"] = _state("state-ready")

    result = await _call(
        handlers,
        "linear_complete_work_item",
        "complete-1",
        issue_id="issue-1",
        summary="Completed locally, but the board moved first.",
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

    completed = await _call(
        handlers,
        "linear_complete_work_item",
        "complete-1",
        issue_id="issue-1",
        summary="Completed.",
    )
    assert completed["result"]["work_item"]["requester_delivery"]["status"] == "pending"

    await mark_work_item_delivery_delivered_for_turn("turn-1")

    execution = (await list_work_item_executions(workspace="pynchy"))[0]
    assert execution.requester_delivery_status == "delivered"
    assert execution.requester_delivered_at is not None
