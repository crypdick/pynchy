"""Tests for safe built-in checks of already-configured services."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from conftest import init_test_database, make_settings

import pynchy.plugins.integrations.operational_canaries as operational_canaries
from pynchy.canaries.api import (
    CanaryRunContext,
    registered_canary_scenarios,
)
from pynchy.canary_contracts import CanaryExercise
from pynchy.host.container_manager.mcp.canary_client import McpCanaryToolError
from pynchy.host.container_manager.mcp.google_canaries import (
    GoogleCalendarRoundTripCanary,
    GoogleDriveRoundTripCanary,
    GoogleMcpCanaryError,
)
from pynchy.host.container_manager.security.artifact_canaries import FileSecretTaintCanary
from pynchy.host.orchestrator.plugin_configuration import configure_builtin_canaries
from pynchy.plugins.integrations.linear import WorkspaceContext
from pynchy.plugins.integrations.operational_canaries import (
    CalendarRoundTripCanary,
    LinearWorkspaceRoundTripCanary,
    ProtonMailRoundTripCanary,
    linear_client_context,
    proton_client_factory,
)
from pynchy.plugins.integrations.proton_bridge import (
    ProtonMailbox,
    ProtonMailboxList,
    ProtonMailDelivery,
    ProtonMailList,
    ProtonMessage,
    ProtonMessageEnvelope,
)
from pynchy.process_environment import filtered_process_environment
from pynchy.security_canary_ids import SECURITY_CANARY_IDS

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
    scenario = CalendarRoundTripCanary(
        "canary",
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


@pytest.mark.asyncio
async def test_calendar_canary_fails_when_service_returns_an_error():
    scenario = CalendarRoundTripCanary(
        "canary",
        list_calendars=AsyncMock(return_value={"error": "unavailable"}),
    )

    with pytest.raises(operational_canaries.CanaryServiceError, match="rejected"):
        await scenario.exercise(_context("calendar.round.trip"))


@pytest.mark.asyncio
async def test_linear_canary_requires_a_configured_account():
    with pytest.raises(operational_canaries.CanaryServiceError, match="does not select"):
        async with linear_client_context(None)():
            pass


def test_proton_canary_requires_mcp_tool_configuration():
    with pytest.raises(operational_canaries.CanaryServiceError, match="requires an MCP"):
        proton_client_factory(None)()


@pytest.mark.asyncio
async def test_file_secret_taint_canary_exercises_and_verifies_artifact_security():
    await init_test_database()
    scenario = FileSecretTaintCanary()

    exercise = await scenario.exercise(_context("security.file-secret-taint"))
    evidence = await scenario.verify(_context("security.file-secret-taint"), exercise)

    assert evidence == (
        "security:artifact-ipc:allow",
        "security:taint:credential:sticky",
    )
    assert await scenario.cleanup(_context("security.file-secret-taint"), exercise) == ()


@pytest.mark.asyncio
async def test_file_secret_taint_canary_rejects_unexpected_artifacts():
    with pytest.raises(RuntimeError, match="sticky credential taint"):
        await FileSecretTaintCanary().verify(
            _context("security.file-secret-taint"),
            CanaryExercise(artifact=object()),
        )


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

    async def search_issues(
        self, query: str, *, team_id: str, first: int = 50
    ) -> list[dict[str, object]]:
        assert query == "Pynchy canary issue test-run"
        assert team_id == "team-1"
        assert first == 1
        return [{"id": "issue-1"}]

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
    monkeypatch.setattr(operational_canaries, "select_team", select_team)
    monkeypatch.setattr(operational_canaries, "list_workspace_todos", list_todos)
    monkeypatch.setattr(operational_canaries, "create_workspace_todo", create_todo)
    monkeypatch.setattr(operational_canaries, "move_workspace_todo", move_todo)
    scenario = LinearWorkspaceRoundTripCanary(
        "CANARY",
        WorkspaceContext(folder="canary-workspace", name="Canary Workspace"),
        client_context=client_context,
    )

    exercise = await scenario.exercise(_context("linear.workspace.round.trip"))
    verified = await scenario.verify(_context("linear.workspace.round.trip"), exercise)
    cleaned = await scenario.cleanup(_context("linear.workspace.round.trip"), exercise)

    assert client.created[0]["team_id"] == "team-1"
    assert move_todo.await_args.kwargs["status"] == "done"
    assert client.deleted == ["issue-1", "todo-1"]
    assert all(ref.startswith("linear:") for ref in (*verified, *cleaned))


@pytest.mark.asyncio
async def test_linear_canary_cleans_issue_when_title_search_misses_it(monkeypatch):
    client = _FakeLinearClient()
    client.search_issues = AsyncMock(return_value=[])

    @asynccontextmanager
    async def client_context():
        yield client

    monkeypatch.setattr(
        operational_canaries, "select_team", AsyncMock(return_value={"id": "team-1"})
    )
    scenario = LinearWorkspaceRoundTripCanary(
        "CANARY",
        WorkspaceContext(folder="canary-workspace", name="Canary Workspace"),
        client_context=client_context,
    )

    with pytest.raises(operational_canaries.CanaryServiceError, match="title search"):
        await scenario.exercise(_context("linear.workspace.round.trip"))

    assert client.deleted == ["issue-1"]


@pytest.mark.asyncio
async def test_linear_canary_cleans_an_issue_when_todo_creation_fails(monkeypatch):
    client = _FakeLinearClient()

    @asynccontextmanager
    async def client_context():
        yield client

    monkeypatch.setattr(
        operational_canaries,
        "select_team",
        AsyncMock(return_value={"id": "team-1"}),
    )
    monkeypatch.setattr(
        operational_canaries,
        "list_workspace_todos",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        operational_canaries,
        "create_workspace_todo",
        AsyncMock(side_effect=RuntimeError("provider failed")),
    )
    scenario = LinearWorkspaceRoundTripCanary(
        "CANARY",
        WorkspaceContext(folder="canary-workspace", name="Canary Workspace"),
        client_context=client_context,
    )

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

    scenario = GoogleCalendarRoundTripCanary(
        "gcal.canary", "pynchy-canary", client_context=client_context
    )

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


@pytest.mark.action(
    "calendar.google.calendar.list",
    "calendar.google.event.list",
    "calendar.google.event.create",
    "calendar.google.event.read",
    "calendar.google.event.delete",
)
@pytest.mark.asyncio
async def test_google_calendar_canary_rejects_an_incomplete_mcp_tool_catalog():
    client = _FakeGoogleMcpClient({"list-calendars", "list-events", "create-event", "get-event"})

    @asynccontextmanager
    async def client_context(_server_name: str):
        yield client

    scenario = GoogleCalendarRoundTripCanary(
        "gcal.canary", "pynchy-canary", client_context=client_context
    )

    with pytest.raises(GoogleMcpCanaryError, match="missing required operational tools"):
        await scenario.exercise(_context("calendar.google.round.trip"))


@pytest.mark.action("drive.google.file.search", "drive.google.file.read")
@pytest.mark.asyncio
async def test_google_drive_canary_searches_and_reads_a_configured_fixture(monkeypatch):
    client = _FakeGoogleMcpClient({"gdrive_search", "gdrive_read_file"})

    @asynccontextmanager
    async def client_context(server_name: str):
        assert server_name == "gdrive.canary"
        yield client

    scenario = GoogleDriveRoundTripCanary(
        "gdrive.canary",
        "pynchy-canary-fixture",
        "fixture-file-id",
        client_context=client_context,
    )

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


@pytest.mark.asyncio
async def test_google_canary_requires_the_managed_mcp_manager(monkeypatch) -> None:
    monkeypatch.setattr(
        "pynchy.host.container_manager.mcp.google_canaries.get_mcp_manager",
        lambda: None,
    )
    scenario = GoogleCalendarRoundTripCanary("gcal.canary", "pynchy-canary")

    with pytest.raises(GoogleMcpCanaryError, match="manager is not available"):
        await scenario.exercise(_context("calendar.google.round.trip"))


@pytest.mark.asyncio
async def test_google_canary_reports_an_unavailable_managed_mcp_server(monkeypatch) -> None:
    manager = AsyncMock()
    manager.get_canary_server_endpoint.side_effect = TimeoutError("startup timed out")
    monkeypatch.setattr(
        "pynchy.host.container_manager.mcp.google_canaries.get_mcp_manager",
        lambda: manager,
    )
    scenario = GoogleDriveRoundTripCanary(
        "gdrive.canary", "pynchy-canary-fixture", "fixture-file-id"
    )

    with pytest.raises(GoogleMcpCanaryError, match="server is not available"):
        await scenario.exercise(_context("drive.google.round.trip"))


@pytest.mark.asyncio
async def test_google_canary_uses_the_managed_mcp_client_context(monkeypatch) -> None:
    manager = AsyncMock()
    manager.get_canary_server_endpoint.return_value = "http://127.0.0.1:8474/mcp"
    client = _FakeGoogleMcpClient({"gdrive_search", "gdrive_read_file"})

    @asynccontextmanager
    async def managed_client(endpoint: str):
        assert endpoint == "http://127.0.0.1:8474/mcp"
        yield client

    monkeypatch.setattr(
        "pynchy.host.container_manager.mcp.google_canaries.get_mcp_manager",
        lambda: manager,
    )
    monkeypatch.setattr(
        "pynchy.host.container_manager.mcp.google_canaries.McpCanaryClient",
        managed_client,
    )
    scenario = GoogleDriveRoundTripCanary(
        "gdrive.canary", "pynchy-canary-fixture", "fixture-file-id"
    )

    exercise = await scenario.exercise(_context("drive.google.round.trip"))

    assert exercise.evidence_refs[0] == "google-drive:search:completed"
    assert exercise.evidence_refs[1].startswith("google-drive:file:read:")


@pytest.mark.asyncio
async def test_google_canaries_reject_wrong_exercise_artifact_types() -> None:
    exercise = CanaryExercise(artifact=object())
    calendar = GoogleCalendarRoundTripCanary(
        "gcal.canary", "pynchy-canary", client_context=AsyncMock()
    )
    drive = GoogleDriveRoundTripCanary(
        "gdrive.canary", "pynchy-canary-fixture", "fixture-file-id", client_context=AsyncMock()
    )

    with pytest.raises(GoogleMcpCanaryError, match="Calendar canary artifact"):
        await calendar.verify(_context("calendar.google.round.trip"), exercise)
    with pytest.raises(GoogleMcpCanaryError, match="Drive canary artifact"):
        await drive.verify(_context("drive.google.round.trip"), exercise)


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
    scenario = ProtonMailRoundTripCanary(
        "INBOX",
        "canary@example.test",
        client_factory=lambda: client,
    )

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


async def test_proton_canary_uses_the_configured_mcp_environment(monkeypatch):
    captured_environment: dict[str, str] | None = None

    def create_client(*, environment: dict[str, str]):
        nonlocal captured_environment
        captured_environment = environment
        return _FakeProtonClient()

    monkeypatch.setenv("PYNCHY_PROTON_BRIDGE_USERNAME", "mail@example.test")
    monkeypatch.setenv("PYNCHY_PROTON_BRIDGE_PASSWORD_COMMAND", _TEST_PASSWORD_COMMAND)
    monkeypatch.setenv("PYNCHY_PROTON_BRIDGE_IMAP_PORT", "2143")
    monkeypatch.setenv("UNRELATED_HOST_TOKEN", "must-not-leak")
    monkeypatch.setattr(operational_canaries, "create_proton_mail_client", create_client)

    environment = filtered_process_environment(
        {
            "PYNCHY_PROTON_BRIDGE_USERNAME": "mail@example.test",
            "PYNCHY_PROTON_BRIDGE_PASSWORD_COMMAND": _TEST_PASSWORD_COMMAND,
            "PYNCHY_PROTON_BRIDGE_IMAP_PORT": "2143",
        }
    )
    await ProtonMailRoundTripCanary(
        "INBOX",
        "canary@example.test",
        client_factory=proton_client_factory(environment),
    ).exercise(_context("proton.mail.round.trip"))

    assert captured_environment is not None
    assert captured_environment["PYNCHY_PROTON_BRIDGE_USERNAME"] == "mail@example.test"
    assert captured_environment["PYNCHY_PROTON_BRIDGE_PASSWORD_COMMAND"] == _TEST_PASSWORD_COMMAND
    assert captured_environment["PYNCHY_PROTON_BRIDGE_IMAP_PORT"] == "2143"
    assert "UNRELATED_HOST_TOKEN" not in captured_environment


def test_built_in_operational_canaries_register_only_safe_supported_services():
    configure_builtin_canaries(make_settings())
    assert set(registered_canary_scenarios()) == {
        "calendar.round.trip",
        "calendar.google.round.trip",
        "drive.google.round.trip",
        "linear.workspace.round.trip",
        "proton.mail.round.trip",
        *SECURITY_CANARY_IDS,
    }
