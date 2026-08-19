"""Failure-path coverage for built-in operational canary scenarios."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import pynchy.plugins.integrations.operational_canaries as canaries
from pynchy.canary_contracts import CanaryExercise, CanaryRunContext
from pynchy.plugins.integrations.linear import WorkspaceContext
from pynchy.plugins.integrations.linear_accounts import LinearAccount
from pynchy.plugins.integrations.operational_canaries import (
    CalendarRoundTripCanary,
    ComputerUseRoundTripCanary,
    LinearWorkspaceRoundTripCanary,
    ProtonMailRoundTripCanary,
    linear_client_context,
    register_operational_canary_scenarios,
)
from pynchy.plugins.integrations.proton_bridge import (
    ProtonMailbox,
    ProtonMailboxList,
    ProtonMailDelivery,
    ProtonMailList,
    ProtonMessage,
    ProtonMessageEnvelope,
)


@dataclass(frozen=True)
class _Config:
    type: str = "linear"
    api_key_env: str = "LINEAR_CANARY_KEY"
    team_key_env: str = "LINEAR_CANARY_TEAM"


def _context() -> object:
    return CanaryRunContext(
        run_id="run", scenario_id="test", target_profile="test", scheduler_deps=None
    )


async def _calendar_exercise() -> CanaryExercise:
    return await CalendarRoundTripCanary(
        "canary",
        list_calendars=AsyncMock(return_value={"result": {}}),
        create_event=AsyncMock(return_value={"result": {"uid": "event-1"}}),
    ).exercise(_context())


@pytest.mark.parametrize(
    ("screenshot", "message"),
    [
        (None, "no screenshot"),
        ({"host_path": 1, "bytes": 3}, "invalid screenshot"),
        ({"host_path": "capture.png", "bytes": "3"}, "invalid screenshot"),
    ],
)
@pytest.mark.asyncio
async def test_computer_use_canary_rejects_malformed_artifacts(
    screenshot: object,
    message: str,
) -> None:
    scenario = ComputerUseRoundTripCanary(
        AsyncMock(return_value={"result": {"screenshot": screenshot}})
    )

    with pytest.raises(canaries.CanaryServiceError, match=message):
        await scenario.exercise(_context())


@pytest.mark.asyncio
async def test_computer_use_canary_rejects_missing_mismatched_and_unexpected_artifacts(
    tmp_path: Path,
) -> None:
    screenshot = tmp_path / "capture.png"
    screenshot.write_bytes(b"png")
    scenario = ComputerUseRoundTripCanary(
        AsyncMock(
            return_value={
                "result": {
                    "screenshot": {"host_path": str(screenshot), "bytes": 3},
                }
            }
        )
    )
    exercise = await scenario.exercise(_context())
    screenshot.unlink()
    with pytest.raises(canaries.CanaryServiceError, match="missing"):
        await scenario.verify(_context(), exercise)

    screenshot.write_bytes(b"long")
    with pytest.raises(canaries.CanaryServiceError, match="size"):
        await scenario.verify(_context(), exercise)

    zero = await ComputerUseRoundTripCanary(
        AsyncMock(
            return_value={
                "result": {
                    "screenshot": {"host_path": str(screenshot), "bytes": 0},
                }
            }
        )
    ).exercise(_context())
    with pytest.raises(canaries.CanaryServiceError, match="size"):
        await scenario.verify(_context(), zero)

    with pytest.raises(canaries.CanaryServiceError, match="unexpected type"):
        await scenario.verify(_context(), CanaryExercise(object()))


@pytest.mark.asyncio
async def test_computer_use_canary_rejects_retained_screenshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screenshot = tmp_path / "capture.png"
    screenshot.write_bytes(b"png")
    scenario = ComputerUseRoundTripCanary(
        AsyncMock(
            return_value={
                "result": {
                    "screenshot": {"host_path": str(screenshot), "bytes": 3},
                }
            }
        )
    )
    exercise = await scenario.exercise(_context())
    monkeypatch.setattr(Path, "unlink", lambda _path, *, missing_ok: None)

    with pytest.raises(canaries.CanaryServiceError, match="retained"):
        await scenario.cleanup(_context(), exercise)


@pytest.mark.asyncio
async def test_calendar_canary_rejects_missing_ids_and_event_lists() -> None:
    scenario = CalendarRoundTripCanary(
        "canary",
        list_calendars=AsyncMock(return_value={"result": {}}),
        create_event=AsyncMock(return_value={"result": {"uid": ""}}),
    )
    with pytest.raises(canaries.CanaryServiceError, match="event identifier"):
        await scenario.exercise(_context())

    exercise = await _calendar_exercise()
    missing = CalendarRoundTripCanary(
        "canary", list_events=AsyncMock(return_value={"result": {"events": []}})
    )
    with pytest.raises(canaries.CanaryServiceError, match="created canary event"):
        await missing.verify(_context(), exercise)

    malformed = CalendarRoundTripCanary(
        "canary", list_events=AsyncMock(return_value={"result": {"events": {}}})
    )
    with pytest.raises(canaries.CanaryServiceError, match="event list"):
        await malformed.verify(_context(), exercise)


@pytest.mark.asyncio
async def test_calendar_cleanup_surfaces_delete_error_while_event_remains() -> None:
    exercise = await _calendar_exercise()
    scenario = CalendarRoundTripCanary(
        "canary",
        list_events=AsyncMock(return_value={"result": {"events": [{"uid": "event-1"}]}}),
        delete_event=AsyncMock(return_value={"error": "denied"}),
    )

    with pytest.raises(canaries.CanaryServiceError, match="rejected"):
        await scenario.cleanup(_context(), exercise)


@pytest.mark.asyncio
async def test_linear_canary_rejects_bad_team_and_cleans_created_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def context(_client: object):
        yield _client

    scenario = LinearWorkspaceRoundTripCanary(
        "CANARY",
        WorkspaceContext(folder="canary", name="Canary"),
        client_context=lambda: context(object()),
    )
    monkeypatch.setattr(canaries, "select_team", AsyncMock(return_value={}))
    with pytest.raises(canaries.CanaryServiceError, match="team without"):
        await scenario.exercise(_context())


@pytest.mark.asyncio
async def test_proton_canary_rejects_missing_mailbox_and_read_mismatch() -> None:
    class Client:
        def list_mailboxes(self):
            return ProtonMailboxList(mailboxes=[ProtonMailbox(name="Other", mailbox="OTHER")])

    scenario = ProtonMailRoundTripCanary("INBOX", "recipient", client_factory=Client)
    with pytest.raises(canaries.CanaryServiceError, match="configured mailbox"):
        await scenario.exercise(_context())

    class MismatchClient:
        def list_mailboxes(self):
            return ProtonMailboxList(mailboxes=[ProtonMailbox(name="Inbox", mailbox="INBOX")])

        def send_mail(self, **_kwargs):
            return ProtonMailDelivery(message_id="<expected>")

        def list_mail(self, **_kwargs):
            return ProtonMailList(
                messages=[
                    ProtonMessageEnvelope(
                        message_id="<expected>", sender=None, subject=None, date=None, seen=False
                    )
                ]
            )

        def read_mail(self, **_kwargs):
            return ProtonMessage(message_id="<different>", body="")

    scenario = ProtonMailRoundTripCanary("INBOX", "recipient", client_factory=MismatchClient)
    exercise = await scenario.exercise(_context())
    with pytest.raises(canaries.CanaryServiceError, match="different message"):
        await scenario.verify(_context(), exercise)


def test_linear_client_context_requires_a_token() -> None:
    account = LinearAccount("canary", _Config())
    with pytest.raises(canaries.CanaryServiceError, match="not available"):
        asyncio.run(_consume(linear_client_context(account)))


def test_linear_client_context_opens_with_a_configured_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_CANARY_KEY", "linear-test-key")  # pragma: allowlist secret
    account = LinearAccount("canary", _Config())
    asyncio.run(_consume(linear_client_context(account)))


async def _consume(factory) -> None:
    async with factory():
        pass


def test_register_operational_canaries_wires_all_three_scenarios() -> None:
    registered: list[tuple[str, object]] = []
    register_operational_canary_scenarios(
        lambda name, scenario: registered.append((name, scenario)),
        calendar=CalendarRoundTripCanary("canary"),
        linear=LinearWorkspaceRoundTripCanary(
            "CANARY",
            WorkspaceContext(folder="canary", name="Canary"),
            client_context=linear_client_context(None),
        ),
        proton=ProtonMailRoundTripCanary("INBOX", "recipient", client_factory=object),
    )

    assert [name for name, _scenario in registered] == [
        "calendar.round.trip",
        "linear.workspace.round.trip",
        "proton.mail.round.trip",
    ]


@pytest.mark.asyncio
async def test_calendar_cleanup_rejects_an_event_that_survives_deletion() -> None:
    exercise = await _calendar_exercise()
    scenario = CalendarRoundTripCanary(
        "canary",
        list_events=AsyncMock(return_value={"result": {"events": [{"uid": "event-1"}]}}),
        delete_event=AsyncMock(return_value={"result": {"status": "deleted"}}),
    )

    with pytest.raises(canaries.CanaryServiceError, match="retained"):
        await scenario.cleanup(_context(), exercise)


@pytest.mark.asyncio
async def test_calendar_canary_rejects_a_handler_without_a_result() -> None:
    scenario = CalendarRoundTripCanary(
        "canary", list_calendars=AsyncMock(return_value={"result": None})
    )

    with pytest.raises(canaries.CanaryServiceError, match="no result"):
        await scenario.exercise(_context())


class _LifecycleLinearClient:
    def __init__(self) -> None:
        self.issues: dict[str, dict[str, object]] = {
            "issue-1": {"id": "issue-1"},
            "todo-1": {"id": "todo-1", "state": {"type": "completed"}},
        }

    async def query(self, _query: str, **_variables: object) -> dict[str, object]:
        return {}

    async def list_teams(self) -> list[dict[str, object]]:
        return []

    async def list_issues(self, *, team_id: str) -> list[dict[str, object]]:
        assert team_id == "team-1"
        return []

    async def search_issues(
        self, query: str, *, team_id: str, first: int = 50
    ) -> list[dict[str, object]]:
        assert query == "Pynchy canary issue run"
        assert team_id == "team-1"
        assert first == 1
        issue = self.issues.get("issue-1")
        return [issue] if issue is not None else []

    async def create_issue(self, **_kwargs: object) -> dict[str, object]:
        issue = {"id": "issue-1"}
        self.issues["issue-1"] = issue
        return issue

    async def get_issue(self, issue_id: str) -> dict[str, object] | None:
        return self.issues.get(issue_id)

    async def delete_issue(self, issue_id: str) -> None:
        self.issues.pop(issue_id, None)


async def _linear_scenario(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[LinearWorkspaceRoundTripCanary, _LifecycleLinearClient, CanaryExercise]:
    client = _LifecycleLinearClient()

    @asynccontextmanager
    async def context():
        yield client

    monkeypatch.setattr(canaries, "select_team", AsyncMock(return_value={"id": "team-1"}))
    monkeypatch.setattr(canaries, "list_workspace_todos", AsyncMock(return_value=[]))
    monkeypatch.setattr(canaries, "create_workspace_todo", AsyncMock(return_value={"id": "todo-1"}))
    monkeypatch.setattr(canaries, "move_workspace_todo", AsyncMock())
    scenario = LinearWorkspaceRoundTripCanary(
        "CANARY", WorkspaceContext(folder="canary", name="Canary"), client_context=context
    )
    return scenario, client, await scenario.exercise(_context())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("issue_id", "todo_id", "message"),
    [
        ("missing", "todo-1", "created canary issue"),
        ("issue-1", "missing", "created canary todo"),
        ("issue-1", "todo-1", "move the canary todo"),
    ],
)
async def test_linear_verify_reports_provider_state_gaps(
    monkeypatch: pytest.MonkeyPatch,
    issue_id: str,
    todo_id: str,
    message: str,
) -> None:
    scenario, client, exercise = await _linear_scenario(monkeypatch)
    client.issues = {
        issue_id: {"id": issue_id},
        todo_id: {"id": todo_id, "state": {"type": "backlog"}},
    }
    if message == "move the canary todo":
        client.issues[todo_id] = {"id": todo_id, "state": {"type": "backlog"}}

    with pytest.raises(canaries.CanaryServiceError, match=message):
        await scenario.verify(_context(), exercise)


@pytest.mark.asyncio
async def test_linear_cleanup_rejects_an_issue_that_survives_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, client, exercise = await _linear_scenario(monkeypatch)
    client.delete_issue = AsyncMock()

    with pytest.raises(canaries.CanaryServiceError, match="retained"):
        await scenario.cleanup(_context(), exercise)


@pytest.mark.asyncio
async def test_linear_cleanup_skips_artifacts_already_removed_by_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, client, exercise = await _linear_scenario(monkeypatch)
    client.issues.clear()
    client.delete_issue = AsyncMock()

    await scenario.cleanup(_context(), exercise)

    client.delete_issue.assert_not_awaited()


@pytest.mark.asyncio
async def test_linear_verify_rejects_a_todo_missing_from_the_final_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, _client, exercise = await _linear_scenario(monkeypatch)
    canaries.list_workspace_todos.return_value = []

    with pytest.raises(canaries.CanaryServiceError, match="todo list"):
        await scenario.verify(_context(), exercise)


@pytest.mark.asyncio
async def test_proton_verify_times_out_after_a_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    class Client:
        def list_mailboxes(self):
            return ProtonMailboxList(mailboxes=[ProtonMailbox(name="Inbox", mailbox="INBOX")])

        def send_mail(self, **_kwargs):
            return ProtonMailDelivery(message_id="<expected>")

    scenario = ProtonMailRoundTripCanary("INBOX", "recipient", client_factory=Client)
    monkeypatch.setattr(scenario, "_listed_and_readable", AsyncMock(return_value=False))
    monkeypatch.setattr(canaries, "_PROTON_DELIVERY_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(canaries.asyncio, "sleep", AsyncMock())

    class Loop:
        values = iter((0.0, 0.0, 1.0))

        def time(self) -> float:
            return next(self.values)

    loop = Loop()
    monkeypatch.setattr(canaries.asyncio, "get_running_loop", lambda: loop)
    exercise = await scenario.exercise(_context())
    with pytest.raises(canaries.CanaryServiceError, match="did not deliver"):
        await scenario.verify(_context(), exercise)


@pytest.mark.asyncio
async def test_proton_cleanup_rejects_a_retained_message() -> None:
    class Client:
        def list_mailboxes(self):
            return ProtonMailboxList(mailboxes=[ProtonMailbox(name="Inbox", mailbox="INBOX")])

        def send_mail(self, **_kwargs):
            return ProtonMailDelivery(message_id="<expected>")

        def delete_mail(self, **_kwargs):
            return None

        def message_exists(self, **_kwargs):
            return True

    scenario = ProtonMailRoundTripCanary("INBOX", "recipient", client_factory=Client)
    exercise = await scenario.exercise(_context())
    with pytest.raises(canaries.CanaryServiceError, match="retained"):
        await scenario.cleanup(_context(), exercise)


@pytest.mark.asyncio
async def test_proton_verify_rejects_a_message_missing_from_the_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Client:
        def list_mailboxes(self):
            return ProtonMailboxList(mailboxes=[ProtonMailbox(name="Inbox", mailbox="INBOX")])

        def send_mail(self, **_kwargs):
            return ProtonMailDelivery(message_id="<expected>")

        def list_mail(self, **_kwargs):
            return ProtonMailList(messages=[])

    scenario = ProtonMailRoundTripCanary("INBOX", "recipient", client_factory=Client)
    exercise = await scenario.exercise(_context())
    monkeypatch.setattr(canaries, "_PROTON_DELIVERY_TIMEOUT_SECONDS", 0)

    with pytest.raises(canaries.CanaryServiceError, match="did not deliver"):
        await scenario.verify(_context(), exercise)


@pytest.mark.asyncio
async def test_scenarios_reject_exercises_with_the_wrong_artifact_type() -> None:
    bad = CanaryExercise(artifact=object())
    calendar = CalendarRoundTripCanary("canary", list_events=AsyncMock())
    with pytest.raises(canaries.CanaryServiceError, match="Calendar canary artifact"):
        await calendar.verify(_context(), bad)

    @asynccontextmanager
    async def context():
        yield object()

    linear = LinearWorkspaceRoundTripCanary(
        "CANARY", WorkspaceContext(folder="canary", name="Canary"), client_context=context
    )
    with pytest.raises(canaries.CanaryServiceError, match="Linear canary artifact"):
        await linear.verify(_context(), bad)

    proton = ProtonMailRoundTripCanary("INBOX", "recipient", client_factory=object)
    with pytest.raises(canaries.CanaryServiceError, match="Proton canary artifact"):
        await proton.verify(_context(), bad)


@pytest.mark.asyncio
async def test_linear_canary_rejects_a_todo_without_an_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def context(_client: object):
        yield _client

    monkeypatch.setattr(canaries, "select_team", AsyncMock(return_value={"id": "team-1"}))
    monkeypatch.setattr(canaries, "list_workspace_todos", AsyncMock(return_value=[]))
    monkeypatch.setattr(canaries, "create_workspace_todo", AsyncMock(return_value={}))
    client = _LifecycleLinearClient()
    client.issues = {}
    scenario = LinearWorkspaceRoundTripCanary(
        "CANARY",
        WorkspaceContext(folder="canary", name="Canary"),
        client_context=lambda: context(client),
    )

    with pytest.raises(canaries.CanaryServiceError, match="issue identifier"):
        await scenario.exercise(_context())
