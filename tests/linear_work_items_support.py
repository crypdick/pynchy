"""Hermetic tests for Linear authority, leasing, and generic agent actions."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest

from pynchy.agent_protocol.api import (
    InFlightTurn,
    InFlightWorkKind,
)
from pynchy.plugins.integrations.linear_boards import LinearWorkspaceBoard
from pynchy.plugins.integrations.linear_client import LinearClient
from pynchy.plugins.integrations.linear_work_item_actions import host_action_registration
from pynchy.plugins.integrations.linear_work_item_provider import (
    WorkItemLeaseRequest,
    acquire_work_item_lease,
)
from pynchy.state import (
    begin_in_flight_turn,
    init_test_database,
)


@dataclass
class FakeLinearState:
    """In-memory Linear boundary, including an accepted write with a lost response."""

    issue: dict[str, Any]
    fail_after_update: bool = False
    update_calls: int = 0
    attachments: list[dict[str, object]] = field(default_factory=list)
    attachment_success: bool = True

    async def get_issue(self, issue_id: str) -> dict[str, Any] | None:
        return deepcopy(self.issue) if issue_id == self.issue["id"] else None

    async def query(self, _query: str, **variables: object) -> dict[str, Any]:
        if "attachmentsForURL" in _query:
            return {
                "attachmentsForURL": {
                    "nodes": [
                        {**attachment, "issue": deepcopy(self.issue)}
                        for attachment in self.attachments
                        if attachment["url"] == variables["url"]
                    ]
                }
            }
        if "attachmentCreate" in _query:
            if not self.attachment_success:
                return {"attachmentCreate": {"success": False}}
            attachment = {
                "id": f"attachment-{len(self.attachments) + 1}",
                "url": variables["url"],
                "title": variables["title"],
                "subtitle": variables.get("subtitle"),
            }
            self.attachments.append(attachment)
            return {"attachmentCreate": {"success": True, "attachment": attachment}}
        self.update_calls += 1
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


async def _begin_turn(*, turn_id: str = "turn-1", input_source: str = "user") -> None:
    await begin_in_flight_turn(
        InFlightTurn(
            turn_id=turn_id,
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
