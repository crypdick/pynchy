"""Tests for safe built-in checks of already-configured services."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from conftest import make_settings

from pynchy.canaries import CanaryRunContext, registered_canary_scenarios
from pynchy.config import CanaryConfig
from pynchy.config.models import McpTool, McpToolConfig
from pynchy.google_mcp_canaries import GoogleCalendarRoundTripCanary, GoogleDriveRoundTripCanary
from pynchy.host.container_manager.mcp.canary_client import McpCanaryToolError
from pynchy.operational_canaries import (
    CalendarRoundTripCanary,
    LinearWorkspaceRoundTripCanary,
    ProtonMailRoundTripCanary,
)
from pynchy.plugins.integrations.proton_bridge import (
    ProtonMailbox,
    ProtonMailboxList,
    ProtonMailDelivery,
    ProtonMailList,
    ProtonMessage,
    ProtonMessageEnvelope,
)

_TEST_PASSWORD_COMMAND = "read-bridge-password"  # noqa: S105  # pragma: allowlist secret


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


class _FakeGoogleMcpClient:
    def __init__(self, tools: set[str]) -> None:
        self.tools = tools
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.events: set[str] = set()

    async def list_tool_names(self) -> set[str]:
        return self.tools

    async def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append((name, arguments))
        if name == "create-event":
            self.events.add(str(arguments["eventId"]))
        if name == "delete-event":
            self.events.discard(str(arguments["eventId"]))
        if name == "get-event" and str(arguments["eventId"]) not in self.events:
            raise McpCanaryToolError("event not found")
        return {"isError": False}


@pytest.mark.action(
    "calendar.google.calendar.list",
    "calendar.google.event.list",
    "calendar.google.event.create",
    "calendar.google.event.read",
    "calendar.google.event.delete",
)
@pytest.mark.asyncio
async def test_google_calendar_canary_uses_real_mcp_tool_contract_and_removes_event(monkeypatch):
    client = _FakeGoogleMcpClient(
        {"list-calendars", "list-events", "create-event", "get-event", "delete-event"}
    )

    @asynccontextmanager
    async def client_context(server_name: str):
        assert server_name == "gcal.canary"
        yield client

    monkeypatch.setattr(
        "pynchy.google_mcp_canaries.get_settings",
        lambda: make_settings(
            canary=CanaryConfig(
                google_calendar_server="gcal.canary", google_calendar_id="pynchy-canary"
            )
        ),
    )
    scenario = GoogleCalendarRoundTripCanary(client_context=client_context)

    exercise = await scenario.exercise(_context("calendar.google.round.trip"))
    verified = await scenario.verify(_context("calendar.google.round.trip"), exercise)
    cleaned = await scenario.cleanup(_context("calendar.google.round.trip"), exercise)

    created = next(arguments for name, arguments in client.calls if name == "create-event")
    assert created["calendarId"] == "pynchy-canary"
    assert created["sendUpdates"] == "none"
    assert str(created["eventId"]).isalnum()
    assert set(str(created["eventId"])) <= set("0123456789abcdefghijklmnopqrstuv")
    assert created["eventId"] not in client.events
    assert all(ref.startswith("google-calendar:") for ref in (*verified, *cleaned))


@pytest.mark.action("drive.google.file.search", "drive.google.file.read")
@pytest.mark.asyncio
async def test_google_drive_canary_searches_and_reads_a_configured_fixture(monkeypatch):
    client = _FakeGoogleMcpClient({"gdrive_search", "gdrive_read_file"})

    @asynccontextmanager
    async def client_context(server_name: str):
        assert server_name == "gdrive.canary"
        yield client

    monkeypatch.setattr(
        "pynchy.google_mcp_canaries.get_settings",
        lambda: make_settings(
            canary=CanaryConfig(
                google_drive_server="gdrive.canary",
                google_drive_probe_query="pynchy-canary-fixture",
                google_drive_file_id="fixture-file-id",
            )
        ),
    )
    scenario = GoogleDriveRoundTripCanary(client_context=client_context)

    exercise = await scenario.exercise(_context("drive.google.round.trip"))
    verified = await scenario.verify(_context("drive.google.round.trip"), exercise)
    cleaned = await scenario.cleanup(_context("drive.google.round.trip"), exercise)

    assert client.calls == [
        ("gdrive_search", {"query": "pynchy-canary-fixture", "pageSize": 1}),
        ("gdrive_read_file", {"fileId": "fixture-file-id"}),
        ("gdrive_read_file", {"fileId": "fixture-file-id"}),
    ]
    assert verified[0].startswith("google-drive:file:verified:")
    assert cleaned == ()


class _FakeProtonClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def list_mailboxes(self) -> ProtonMailboxList:
        self.calls.append(("list_mailboxes", None))
        return ProtonMailboxList(mailboxes=[ProtonMailbox(name="Inbox", mailbox="INBOX")])

    def list_mail(self, **_kwargs: object) -> ProtonMailList:
        self.calls.append(("list_mail", _kwargs))
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
        self.calls.append(("read_mail", _kwargs))
        return ProtonMessage(message_id="<canary@example.test>", body="Safe test body")

    def send_mail(self, **kwargs: object) -> ProtonMailDelivery:
        self.calls.append(("send_mail", kwargs))
        return ProtonMailDelivery(message_id="<canary@example.test>")

    def delete_mail(self, **kwargs: object) -> None:
        self.calls.append(("delete_mail", kwargs))

    def message_exists(self, **kwargs: object) -> bool:
        self.calls.append(("message_exists", kwargs))
        return False


@pytest.mark.asyncio
async def test_proton_canary_sends_receives_reads_and_cleans_without_persisting_content(
    monkeypatch,
):
    client = _FakeProtonClient()
    monkeypatch.setattr(
        "pynchy.operational_canaries.get_settings",
        lambda: make_settings(
            canary=CanaryConfig(proton_mailbox="INBOX", proton_recipient="canary@example.test")
        ),
    )
    scenario = ProtonMailRoundTripCanary(client_factory=lambda: client)

    exercise = await scenario.exercise(_context("proton.mail.round.trip"))
    verified = await scenario.verify(_context("proton.mail.round.trip"), exercise)
    cleaned = await scenario.cleanup(_context("proton.mail.round.trip"), exercise)

    assert all("canary@example.test" not in ref for ref in (*exercise.evidence_refs, *verified))
    assert client.calls[1] == (
        "send_mail",
        {
            "recipients": ["canary@example.test"],
            "subject": "Pynchy canary test-run",
            "body": "Automated Pynchy canary; removed after verification.",
        },
    )
    assert all(ref.startswith("proton:") for ref in cleaned)
    assert (
        "delete_mail",
        {"mailbox": "INBOX", "message_id": "<canary@example.test>"},
    ) in client.calls
    assert (
        "message_exists",
        {"mailbox": "INBOX", "message_id": "<canary@example.test>"},
    ) in client.calls


def test_proton_canary_uses_the_configured_mcp_environment(monkeypatch):
    settings = make_settings(
        tools={
            "proton-mail": McpTool(
                type="mcp",
                mcp=McpToolConfig(
                    runtime="script",
                    command="uv",
                    port=8475,
                    env={
                        "PYNCHY_PROTON_BRIDGE_USERNAME": "mail@example.test",
                        "PYNCHY_PROTON_BRIDGE_PASSWORD_COMMAND": _TEST_PASSWORD_COMMAND,
                    },
                    env_forward={"PYNCHY_PROTON_BRIDGE_IMAP_PORT": "TEST_IMAP_PORT"},
                ),
            )
        }
    )
    captured_environment: dict[str, str] | None = None

    def create_client(*, environment: dict[str, str]):
        nonlocal captured_environment
        captured_environment = environment
        return _FakeProtonClient()

    monkeypatch.setenv("TEST_IMAP_PORT", "2143")
    monkeypatch.setattr("pynchy.operational_canaries.get_settings", lambda: settings)
    monkeypatch.setattr("pynchy.operational_canaries.create_proton_mail_client", create_client)

    ProtonMailRoundTripCanary()._client_factory()

    assert captured_environment is not None
    assert captured_environment["PYNCHY_PROTON_BRIDGE_USERNAME"] == "mail@example.test"
    assert captured_environment["PYNCHY_PROTON_BRIDGE_PASSWORD_COMMAND"] == _TEST_PASSWORD_COMMAND
    assert captured_environment["PYNCHY_PROTON_BRIDGE_IMAP_PORT"] == "2143"


def test_built_in_operational_canaries_register_only_safe_supported_services():
    assert set(registered_canary_scenarios()) == {
        "calendar.round.trip",
        "calendar.google.round.trip",
        "drive.google.round.trip",
        "linear.workspace.round.trip",
        "proton.mail.round.trip",
    }
