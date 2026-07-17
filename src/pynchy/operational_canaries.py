"""Safe real-service canaries for built-in operational integrations."""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

import aiohttp

from pynchy.canaries import CanaryExercise, CanaryRunContext, CanaryScenario
from pynchy.config import get_settings
from pynchy.plugins.integrations.caldav import (
    _handle_create_event,
    _handle_delete_event,
    _handle_list_calendar,
    _handle_list_calendars,
)
from pynchy.plugins.integrations.linear import LinearClient, WorkspaceContext
from pynchy.plugins.integrations.linear_boards import (
    create_workspace_todo,
    list_workspace_todos,
    move_workspace_todo,
    select_team,
)
from pynchy.plugins.integrations.proton_bridge import ProtonMailClient, create_proton_mail_client

_LINEAR_TIMEOUT_SECONDS = 30
_CANARY_EVENT_DURATION = timedelta(minutes=1)
_CANARY_EVENT_LEAD_TIME = timedelta(minutes=10)

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

    async def create_issue(  # noqa: PLR0913, RUF100 - mirrors the built-in Linear client contract.
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
        *,
        list_calendars: ServiceHandler = _handle_list_calendars,
        list_events: ServiceHandler = _handle_list_calendar,
        create_event: ServiceHandler = _handle_create_event,
        delete_event: ServiceHandler = _handle_delete_event,
    ) -> None:
        self._list_calendars = list_calendars
        self._list_events = list_events
        self._create_event = create_event
        self._delete_event = delete_event

    async def exercise(self, context: CanaryRunContext) -> CanaryExercise:
        calendar_name = get_settings().canary.calendar_name
        _service_result(await self._list_calendars({}))
        start = datetime.now(UTC) + _CANARY_EVENT_LEAD_TIME
        end = start + _CANARY_EVENT_DURATION
        result = _service_result(
            await self._create_event(
                {
                    "calendar": calendar_name,
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
            artifact=_CalendarArtifact(calendar_name, event_id, start, end),
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

    def __init__(self, *, client_context: LinearClientContextFactory | None = None) -> None:
        self._client_context = client_context or _linear_client

    async def exercise(self, context: CanaryRunContext) -> CanaryExercise:
        settings = get_settings().canary
        workspace = _linear_workspace(settings.linear_workspace)
        async with self._client_context() as client:
            team = await select_team(client, team_key=settings.linear_team_key)
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
                await list_workspace_todos(
                    client,
                    workspace,
                    team_key=settings.linear_team_key,
                    include_done=True,
                )
                todo = await create_workspace_todo(
                    client,
                    workspace,
                    f"Pynchy canary todo {context.run_id}",
                    team_key=settings.linear_team_key,
                )
                todo_id = _linear_issue_id(todo)
                await move_workspace_todo(
                    client,
                    workspace,
                    issue_id=todo_id,
                    status="done",
                    team_key=settings.linear_team_key,
                )
            except Exception:  # noqa: BLE001, RUF100 - remove any issue created before an incomplete canary exercise.
                await _delete_linear_artifacts(client, (issue_id, todo_id))
                raise
            if todo_id is None:
                raise CanaryServiceError("Linear did not return a created canary todo identifier")
        return CanaryExercise(
            artifact=_LinearArtifact(issue_id, todo_id),
            evidence_refs=(
                _ref("linear:issue:created", issue_id),
                _ref("linear:todo:created", todo_id),
            ),
        )

    async def verify(self, _context: CanaryRunContext, exercise: CanaryExercise) -> tuple[str, ...]:
        artifact = _linear_artifact(exercise)
        settings = get_settings().canary
        workspace = _linear_workspace(settings.linear_workspace)
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
                workspace,
                team_key=settings.linear_team_key,
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


class ProtonMailReadCanary:
    """Verify read-only Proton Bridge mailbox, list, and message access."""

    def __init__(self, *, client_factory: ProtonClientFactory = create_proton_mail_client) -> None:
        self._client_factory = client_factory

    async def exercise(self, _context: CanaryRunContext) -> CanaryExercise:
        mailbox = get_settings().canary.proton_mailbox
        client = self._client_factory()
        mailboxes = await asyncio.to_thread(client.list_mailboxes)
        if mailbox not in {entry.mailbox for entry in mailboxes.mailboxes}:
            raise CanaryServiceError("Proton Bridge did not return the configured mailbox")
        messages = await asyncio.to_thread(
            client.list_mail,
            mailbox=mailbox,
            limit=1,
            offset=0,
            unread=False,
        )
        if not messages.messages or not messages.messages[0].message_id:
            raise CanaryServiceError("Proton Bridge returned no message to verify read access")
        message_id = messages.messages[0].message_id
        return CanaryExercise(
            artifact=_ProtonArtifact(mailbox, message_id),
            evidence_refs=("proton:mailboxes:listed", _ref("proton:message:listed", message_id)),
        )

    async def verify(self, _context: CanaryRunContext, exercise: CanaryExercise) -> tuple[str, ...]:
        artifact = _proton_artifact(exercise)
        message = await asyncio.to_thread(
            self._client_factory().read_mail,
            mailbox=artifact.mailbox,
            message_id=artifact.message_id,
            include_headers=False,
        )
        if message.message_id != artifact.message_id:
            raise CanaryServiceError("Proton Bridge read a different message than it listed")
        return (_ref("proton:message:read", artifact.message_id),)

    async def cleanup(
        self, _context: CanaryRunContext, _exercise: CanaryExercise
    ) -> tuple[str, ...]:
        return ("proton:read-only",)


@asynccontextmanager
async def _linear_client() -> AsyncIterator[LinearClient]:
    token = os.environ.get("LINEAR_API_KEY")
    if not token:
        raise CanaryServiceError("Linear API key is not available to the canary runner")
    timeout = aiohttp.ClientTimeout(total=_LINEAR_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        yield LinearClient(api_key=token, session=session)


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


def _linear_workspace(name: str) -> WorkspaceContext:
    return WorkspaceContext(folder=name, name=name.replace("-", " ").replace("_", " ").title())


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


def register_operational_canary_scenarios(register: CanaryRegistration) -> None:
    """Register checks for integrations that already have operational actions."""
    # Keep credential setup and social posting out of this group: it proves that
    # already-configured services keep working. See docs/architecture/action-coverage.md.
    register("calendar.round.trip", CalendarRoundTripCanary())
    register("linear.workspace.round.trip", LinearWorkspaceRoundTripCanary())
    register("proton.mail.read", ProtonMailReadCanary())
