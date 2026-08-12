"""Safe real-service canaries for built-in operational integrations."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

import aiohttp

from pynchy.canary_contracts import CanaryExercise, CanaryRunContext, CanaryScenario
from pynchy.plugins.integrations.api import (
    LinearAccount,
    LinearClient,
    ProtonMailClient,
    WorkspaceContext,
    WorkspaceTodoProposal,
    _handle_create_event,
    _handle_delete_event,
    _handle_list_calendar,
    _handle_list_calendars,
    create_proton_mail_client,
    create_workspace_todo,
    list_workspace_todos,
    move_workspace_todo,
    select_team,
)

_CANARY_EVENT_DURATION = timedelta(minutes=1)
_CANARY_EVENT_LEAD_TIME = timedelta(minutes=10)
_PROTON_DELIVERY_TIMEOUT_SECONDS = 60
_PROTON_DELIVERY_POLL_SECONDS = 2

type ServiceHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
type ProtonClientFactory = Callable[[], ProtonMailClient]
type CanaryRegistration = Callable[[str, CanaryScenario], None]


class CanaryServiceError(RuntimeError):
    """A configured service declined an operational canary request."""


@runtime_checkable
class _LinearCanaryClient(Protocol):
    """Linear operations required by the lifecycle canary."""

    async def query(self, query: str, **variables: object) -> dict[str, Any]: ...

    async def list_teams(self) -> list[dict[str, Any]]: ...

    async def list_issues(self, *, team_id: str) -> list[dict[str, Any]]: ...

    async def search_issues(
        self, query: str, *, team_id: str, first: int = 50
    ) -> list[dict[str, Any]]: ...

    async def create_issue(  # noqa: PLR0913 - mirrors the built-in Linear client contract.
        self,
        *,
        team_id: str,
        title: str,
        description: str | None = None,
        project_id: str | None = None,
        state_id: str | None = None,
        label_ids: list[str] | None = None,
    ) -> dict[str, Any]: ...

    async def get_issue(self, issue_id: str) -> dict[str, Any] | None: ...

    async def delete_issue(self, issue_id: str) -> None: ...


type LinearClientContextFactory = Callable[[], AbstractAsyncContextManager[_LinearCanaryClient]]


@dataclass(frozen=True)
class _CalendarArtifact:
    calendar_name: str
    event_id: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class _LinearArtifact:
    issue_id: str
    todo_id: str


@dataclass(frozen=True)
class _ProtonArtifact:
    mailbox: str
    message_id: str


class CalendarRoundTripCanary:
    """Create, independently find, then delete a tagged test calendar event."""

    def __init__(
        self,
        calendar_name: str,
        *,
        list_calendars: ServiceHandler = _handle_list_calendars,
        list_events: ServiceHandler = _handle_list_calendar,
        create_event: ServiceHandler = _handle_create_event,
        delete_event: ServiceHandler = _handle_delete_event,
    ) -> None:
        self._calendar_name = calendar_name
        self._list_calendars = list_calendars
        self._list_events = list_events
        self._create_event = create_event
        self._delete_event = delete_event

    async def exercise(self, context: CanaryRunContext) -> CanaryExercise:
        _service_result(await self._list_calendars({}))
        start = datetime.now(UTC) + _CANARY_EVENT_LEAD_TIME
        end = start + _CANARY_EVENT_DURATION
        result = _service_result(
            await self._create_event(
                {
                    "calendar": self._calendar_name,
                    "title": f"Pynchy canary {context.run_id}",
                    "description": "Automated Pynchy canary; removed after verification.",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                }
            )
        )
        event_id = result.get("uid")
        if not isinstance(event_id, str) or not event_id:
            raise CanaryServiceError("Calendar did not return a created event identifier")
        return CanaryExercise(
            artifact=_CalendarArtifact(self._calendar_name, event_id, start, end),
            evidence_refs=("calendar:listed", _ref("calendar:created", event_id)),
        )

    async def verify(self, _context: CanaryRunContext, exercise: CanaryExercise) -> tuple[str, ...]:
        artifact = _calendar_artifact(exercise)
        if not await self._event_exists(artifact):
            raise CanaryServiceError("Calendar did not return the created canary event")
        return (_ref("calendar:verified", artifact.event_id),)

    async def cleanup(
        self, _context: CanaryRunContext, exercise: CanaryExercise
    ) -> tuple[str, ...]:
        artifact = _calendar_artifact(exercise)
        response = await self._delete_event(
            {"calendar": artifact.calendar_name, "event_id": artifact.event_id}
        )
        if "error" in response and await self._event_exists(artifact):
            _service_result(response)
        if await self._event_exists(artifact):
            raise CanaryServiceError("Calendar retained the deleted canary event")
        return (_ref("calendar:deleted", artifact.event_id),)

    async def _event_exists(self, artifact: _CalendarArtifact) -> bool:
        result = _service_result(
            await self._list_events(
                {
                    "calendar": artifact.calendar_name,
                    "start_date": (artifact.start - timedelta(minutes=1)).isoformat(),
                    "end_date": (artifact.end + timedelta(minutes=1)).isoformat(),
                }
            )
        )
        events = result.get("events")
        if not isinstance(events, list):
            raise CanaryServiceError("Calendar did not return an event list")
        return any(
            isinstance(event, dict) and event.get("uid") == artifact.event_id for event in events
        )


class LinearWorkspaceRoundTripCanary:
    """Exercise issue and workspace-todo lifecycle in a configured test target."""

    def __init__(
        self,
        team_key: str,
        workspace: WorkspaceContext,
        *,
        client_context: LinearClientContextFactory,
    ) -> None:
        self._team_key = team_key
        self._workspace = workspace
        self._client_context = client_context

    async def exercise(self, context: CanaryRunContext) -> CanaryExercise:
        async with self._client_context() as client:
            team = await select_team(client, team_key=self._team_key)
            team_id = team.get("id")
            if not isinstance(team_id, str) or not team_id:
                raise CanaryServiceError("Linear selected a team without an identifier")
            await client.list_issues(team_id=team_id)
            issue = await client.create_issue(
                team_id=team_id,
                title=f"Pynchy canary issue {context.run_id}",
                description="Automated Pynchy canary; removed after verification.",
            )
            issue_id = _linear_issue_id(issue)
            todo_id: str | None = None
            try:
                await _verify_linear_title_search(client, team_id, issue_id, context.run_id)
            except Exception:  # remove the issue when the title-search smoke check fails.
                await _delete_linear_artifacts(client, (issue_id,))
                raise
            try:
                await list_workspace_todos(
                    client,
                    self._workspace,
                    team_key=self._team_key,
                    include_done=True,
                )
                todo = await create_workspace_todo(
                    client,
                    self._workspace,
                    WorkspaceTodoProposal(title=f"Pynchy canary todo {context.run_id}"),
                    team_key=self._team_key,
                )
                todo_id = _linear_issue_id(todo)
                await move_workspace_todo(
                    client,
                    self._workspace,
                    issue_id=todo_id,
                    status="done",
                    team_key=self._team_key,
                )
            except Exception:  # remove any issue created before an incomplete canary exercise.
                await _delete_linear_artifacts(client, (issue_id, todo_id))
                raise
        return CanaryExercise(
            artifact=_LinearArtifact(issue_id, todo_id),
            evidence_refs=(
                _ref("linear:issue:created", issue_id),
                _ref("linear:todo:created", todo_id),
            ),
        )

    async def verify(self, _context: CanaryRunContext, exercise: CanaryExercise) -> tuple[str, ...]:
        artifact = _linear_artifact(exercise)
        async with self._client_context() as client:
            if await client.get_issue(artifact.issue_id) is None:
                raise CanaryServiceError("Linear did not return the created canary issue")
            todo = await client.get_issue(artifact.todo_id)
            if todo is None:
                raise CanaryServiceError("Linear did not return the created canary todo")
            state = todo.get("state")
            if not isinstance(state, dict) or state.get("type") != "completed":
                raise CanaryServiceError("Linear did not move the canary todo to done")
            todos = await list_workspace_todos(
                client,
                self._workspace,
                team_key=self._team_key,
                include_done=True,
            )
            if not any(todo.get("id") == artifact.todo_id for todo in todos):
                raise CanaryServiceError("Linear todo list did not return the canary todo")
        return (
            _ref("linear:issue:verified", artifact.issue_id),
            _ref("linear:todo:verified", artifact.todo_id),
        )

    async def cleanup(
        self, _context: CanaryRunContext, exercise: CanaryExercise
    ) -> tuple[str, ...]:
        artifact = _linear_artifact(exercise)
        async with self._client_context() as client:
            await _delete_linear_artifacts(client, (artifact.issue_id, artifact.todo_id))
        return (
            _ref("linear:issue:deleted", artifact.issue_id),
            _ref("linear:todo:deleted", artifact.todo_id),
        )


class ProtonMailRoundTripCanary:
    """Send, list, read, and permanently remove a tagged Bridge message."""

    def __init__(
        self,
        mailbox: str,
        recipient: str,
        *,
        client_factory: ProtonClientFactory,
    ) -> None:
        self._mailbox = mailbox
        self._recipient = recipient
        self._client_factory = client_factory

    async def exercise(self, context: CanaryRunContext) -> CanaryExercise:
        client = self._client_factory()
        mailboxes = await asyncio.to_thread(client.list_mailboxes)
        if self._mailbox not in {entry.mailbox for entry in mailboxes.mailboxes}:
            raise CanaryServiceError("Proton Bridge did not return the configured mailbox")
        delivery = await asyncio.to_thread(
            client.send_mail,
            recipients=[self._recipient],
            subject=f"Pynchy canary {context.run_id}",
            body="Automated Pynchy canary; removed after verification.",
        )
        return CanaryExercise(
            artifact=_ProtonArtifact(self._mailbox, delivery.message_id),
            evidence_refs=(
                "proton:mailboxes:listed",
                _ref("proton:message:sent", delivery.message_id),
            ),
        )

    async def verify(self, _context: CanaryRunContext, exercise: CanaryExercise) -> tuple[str, ...]:
        artifact = _proton_artifact(exercise)
        deadline = asyncio.get_running_loop().time() + _PROTON_DELIVERY_TIMEOUT_SECONDS
        while True:
            if await self._listed_and_readable(artifact):
                return (
                    _ref("proton:message:listed", artifact.message_id),
                    _ref("proton:message:read", artifact.message_id),
                )
            if asyncio.get_running_loop().time() >= deadline:
                raise CanaryServiceError("Proton Bridge did not deliver the sent canary message")
            await asyncio.sleep(_PROTON_DELIVERY_POLL_SECONDS)

    async def cleanup(
        self, _context: CanaryRunContext, exercise: CanaryExercise
    ) -> tuple[str, ...]:
        artifact = _proton_artifact(exercise)
        client = self._client_factory()
        await asyncio.to_thread(
            client.delete_mail,
            mailbox=artifact.mailbox,
            message_id=artifact.message_id,
        )
        if await asyncio.to_thread(
            client.message_exists,
            mailbox=artifact.mailbox,
            message_id=artifact.message_id,
        ):
            raise CanaryServiceError("Proton Bridge retained the deleted canary message")
        return (_ref("proton:message:deleted", artifact.message_id),)

    async def _listed_and_readable(self, artifact: _ProtonArtifact) -> bool:
        client = self._client_factory()
        messages = await asyncio.to_thread(
            client.list_mail,
            mailbox=artifact.mailbox,
            limit=200,
            offset=0,
            unread=False,
        )
        if not any(message.message_id == artifact.message_id for message in messages.messages):
            return False
        message = await asyncio.to_thread(
            client.read_mail,
            mailbox=artifact.mailbox,
            message_id=artifact.message_id,
            include_headers=False,
        )
        if message.message_id != artifact.message_id:
            raise CanaryServiceError("Proton Bridge read a different message than it listed")
        return True


def linear_client_context(account: LinearAccount | None) -> LinearClientContextFactory:
    """Build a credential-scoped Linear client context from composition input."""

    @asynccontextmanager
    async def connect() -> AsyncIterator[LinearClient]:
        if account is None:
            raise CanaryServiceError("Linear canary workspace does not select a Linear account")
        token = account.api_key
        if not token:
            raise CanaryServiceError(
                f"{account.config.api_key_env} is not available to the canary runner"
            )
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            yield LinearClient(api_key=token, session=session)

    return connect


def proton_client_factory(environment: dict[str, str] | None) -> ProtonClientFactory:
    """Build the configured Proton client factory from composition input."""

    def create() -> ProtonMailClient:
        if environment is None:
            raise CanaryServiceError("Proton mail canary requires an MCP tool configuration")
        return create_proton_mail_client(environment=environment)

    return create


def _service_result(response: dict[str, Any]) -> dict[str, Any]:
    if "error" in response:
        raise CanaryServiceError("Service handler rejected canary request")
    result = response.get("result")
    if not isinstance(result, dict):
        raise CanaryServiceError("Service handler returned no result")
    return result


def _calendar_artifact(exercise: CanaryExercise) -> _CalendarArtifact:
    if not isinstance(exercise.artifact, _CalendarArtifact):
        raise CanaryServiceError("Calendar canary artifact has an unexpected type")
    return exercise.artifact


def _linear_artifact(exercise: CanaryExercise) -> _LinearArtifact:
    if not isinstance(exercise.artifact, _LinearArtifact):
        raise CanaryServiceError("Linear canary artifact has an unexpected type")
    return exercise.artifact


def _proton_artifact(exercise: CanaryExercise) -> _ProtonArtifact:
    if not isinstance(exercise.artifact, _ProtonArtifact):
        raise CanaryServiceError("Proton canary artifact has an unexpected type")
    return exercise.artifact


def _linear_issue_id(issue: dict[str, Any]) -> str:
    issue_id = issue.get("id")
    if not isinstance(issue_id, str) or not issue_id:
        raise CanaryServiceError("Linear did not return a created issue identifier")
    return issue_id


async def _verify_linear_title_search(
    client: _LinearCanaryClient,
    team_id: str,
    issue_id: str,
    run_id: str,
) -> None:
    results = await client.search_issues(f"Pynchy canary issue {run_id}", team_id=team_id, first=1)
    if not any(result.get("id") == issue_id for result in results):
        raise CanaryServiceError("Linear title search did not return the created canary issue")


async def _delete_linear_artifacts(
    client: _LinearCanaryClient,
    issue_ids: tuple[str | None, ...],
) -> None:
    for issue_id in issue_ids:
        if issue_id is None:
            continue
        if await client.get_issue(issue_id) is not None:
            await client.delete_issue(issue_id)
        if await client.get_issue(issue_id) is not None:
            raise CanaryServiceError("Linear retained a deleted canary issue")


def _ref(prefix: str, value: str) -> str:
    return f"{prefix}:{hashlib.sha256(value.encode()).hexdigest()[:12]}"


def register_operational_canary_scenarios(
    register: CanaryRegistration,
    *,
    calendar: CalendarRoundTripCanary,
    linear: LinearWorkspaceRoundTripCanary,
    proton: ProtonMailRoundTripCanary,
) -> None:
    """Register checks for integrations that already have operational actions."""
    # Keep credential setup and social posting out of this group: it proves that
    # already-configured services keep working. See docs/architecture/action-coverage.md.
    register("calendar.round.trip", calendar)
    register("linear.workspace.round.trip", linear)
    register("proton.mail.round.trip", proton)
