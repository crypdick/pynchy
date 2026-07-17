"""Tests for safe built-in checks of already-configured services."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from conftest import make_settings

from pynchy.canaries import CanaryRunContext, registered_canary_scenarios
from pynchy.config import CanaryConfig
from pynchy.operational_canaries import (
    CalendarRoundTripCanary,
    LinearWorkspaceRoundTripCanary,
    ProtonMailReadCanary,
)
from pynchy.plugins.integrations.proton_bridge import (
    ProtonMailbox,
    ProtonMailboxList,
    ProtonMailList,
    ProtonMessage,
    ProtonMessageEnvelope,
)


def _context(scenario_id: str) -> CanaryRunContext:
    return CanaryRunContext(
        run_id="test-run",
        scenario_id=scenario_id,
        target_profile="external-canary",
        scheduler_deps=None,
    )


@pytest.mark.asyncio
async def test_calendar_canary_uses_configured_calendar_and_removes_event(monkeypatch):
    list_calendars = AsyncMock(return_value={"result": {"servers": {"test": ["canary"]}}})
    list_events = AsyncMock(
        side_effect=[{"result": {"events": [{"uid": "event-1"}]}}, {"result": {"events": []}}]
    )
    create_event = AsyncMock(return_value={"result": {"uid": "event-1", "status": "created"}})
    delete_event = AsyncMock(return_value={"result": {"uid": "event-1", "status": "deleted"}})
    monkeypatch.setattr(
        "pynchy.operational_canaries.get_settings",
        lambda: make_settings(canary=CanaryConfig(calendar_name="canary")),
    )
    scenario = CalendarRoundTripCanary(
        list_calendars=list_calendars,
        list_events=list_events,
        create_event=create_event,
        delete_event=delete_event,
    )

    exercise = await scenario.exercise(_context("calendar.round.trip"))
    verified = await scenario.verify(_context("calendar.round.trip"), exercise)
    cleaned = await scenario.cleanup(_context("calendar.round.trip"), exercise)

    assert create_event.await_args.args[0]["calendar"] == "canary"
    assert verified[0].startswith("calendar:verified:")
    assert cleaned[0].startswith("calendar:deleted:")
    delete_event.assert_awaited_once_with({"calendar": "canary", "event_id": "event-1"})


class _FakeLinearClient:
    def __init__(self) -> None:
        self.issues: dict[str, dict[str, object]] = {
            "issue-1": {"id": "issue-1", "state": {"type": "backlog"}},
            "todo-1": {"id": "todo-1", "state": {"type": "completed"}},
        }
        self.created: list[dict[str, object]] = []
        self.deleted: list[str] = []

    async def query(self, _query: str, **_variables: object) -> dict[str, object]:
        return {}

    async def list_teams(self) -> list[dict[str, object]]:
        return [{"id": "team-1"}]

    async def list_issues(self, *, team_id: str) -> list[dict[str, object]]:
        assert team_id == "team-1"
        return []

    async def create_issue(self, **kwargs: object) -> dict[str, object]:
        self.created.append(kwargs)
        return {"id": "issue-1"}

    async def get_issue(self, issue_id: str) -> dict[str, object] | None:
        return self.issues.get(issue_id)

    async def delete_issue(self, issue_id: str) -> None:
        self.deleted.append(issue_id)
        self.issues.pop(issue_id, None)


@pytest.mark.asyncio
async def test_linear_canary_exercises_issue_and_todo_lifecycle(monkeypatch):
    client = _FakeLinearClient()

    @asynccontextmanager
    async def client_context():
        yield client

    select_team = AsyncMock(return_value={"id": "team-1"})
    list_todos = AsyncMock(return_value=[{"id": "todo-1"}])
    create_todo = AsyncMock(return_value={"id": "todo-1"})
    move_todo = AsyncMock(return_value={"id": "todo-1", "state": {"type": "completed"}})
    monkeypatch.setattr("pynchy.operational_canaries.select_team", select_team)
    monkeypatch.setattr("pynchy.operational_canaries.list_workspace_todos", list_todos)
    monkeypatch.setattr("pynchy.operational_canaries.create_workspace_todo", create_todo)
    monkeypatch.setattr("pynchy.operational_canaries.move_workspace_todo", move_todo)
    monkeypatch.setattr(
        "pynchy.operational_canaries.get_settings",
        lambda: make_settings(
            canary=CanaryConfig(linear_team_key="CANARY", linear_workspace="canary-workspace")
        ),
    )
    scenario = LinearWorkspaceRoundTripCanary(client_context=client_context)

    exercise = await scenario.exercise(_context("linear.workspace.round.trip"))
    verified = await scenario.verify(_context("linear.workspace.round.trip"), exercise)
    cleaned = await scenario.cleanup(_context("linear.workspace.round.trip"), exercise)

    assert client.created[0]["team_id"] == "team-1"
    assert move_todo.await_args.kwargs["status"] == "done"
    assert client.deleted == ["issue-1", "todo-1"]
    assert all(ref.startswith("linear:") for ref in (*verified, *cleaned))


@pytest.mark.asyncio
async def test_linear_canary_cleans_an_issue_when_todo_creation_fails(monkeypatch):
    client = _FakeLinearClient()

    @asynccontextmanager
    async def client_context():
        yield client

    monkeypatch.setattr(
        "pynchy.operational_canaries.select_team", AsyncMock(return_value={"id": "team-1"})
    )
    monkeypatch.setattr(
        "pynchy.operational_canaries.list_workspace_todos", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        "pynchy.operational_canaries.create_workspace_todo",
        AsyncMock(side_effect=RuntimeError("provider failed")),
    )
    monkeypatch.setattr(
        "pynchy.operational_canaries.get_settings",
        lambda: make_settings(
            canary=CanaryConfig(linear_team_key="CANARY", linear_workspace="canary-workspace")
        ),
    )
    scenario = LinearWorkspaceRoundTripCanary(client_context=client_context)

    with pytest.raises(RuntimeError, match="provider failed"):
        await scenario.exercise(_context("linear.workspace.round.trip"))

    assert client.deleted == ["issue-1"]


class _FakeProtonClient:
    def list_mailboxes(self) -> ProtonMailboxList:
        return ProtonMailboxList(mailboxes=[ProtonMailbox(name="Inbox", mailbox="INBOX")])

    def list_mail(self, **_kwargs: object) -> ProtonMailList:
        return ProtonMailList(
            messages=[
                ProtonMessageEnvelope(
                    message_id="<canary@example.test>",
                    sender="sender@example.test",
                    subject="Canary",
                    date="2026-07-17T00:00:00Z",
                    seen=True,
                )
            ]
        )

    def read_mail(self, **_kwargs: object) -> ProtonMessage:
        return ProtonMessage(message_id="<canary@example.test>", body="Safe test body")


@pytest.mark.asyncio
async def test_proton_canary_does_not_persist_message_content(monkeypatch):
    monkeypatch.setattr(
        "pynchy.operational_canaries.get_settings",
        lambda: make_settings(canary=CanaryConfig(proton_mailbox="INBOX")),
    )
    scenario = ProtonMailReadCanary(client_factory=_FakeProtonClient)

    exercise = await scenario.exercise(_context("proton.mail.read"))
    verified = await scenario.verify(_context("proton.mail.read"), exercise)
    cleaned = await scenario.cleanup(_context("proton.mail.read"), exercise)

    assert all("canary@example.test" not in ref for ref in (*exercise.evidence_refs, *verified))
    assert cleaned == ("proton:read-only",)


def test_built_in_operational_canaries_register_only_safe_supported_services():
    assert set(registered_canary_scenarios()) == {
        "calendar.round.trip",
        "linear.workspace.round.trip",
        "proton.mail.read",
    }
