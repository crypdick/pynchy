"""Provider-boundary operational canaries for Google MCP integrations."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from pynchy.canary_contracts import CanaryExercise, CanaryRunContext, CanaryScenario
from pynchy.host.container_manager.api import (
    McpCanaryClient,
    McpCanaryToolError,
    get_mcp_manager,
)

type CanaryRegistration = Callable[[str, CanaryScenario], None]

_CANARY_EVENT_DURATION = timedelta(minutes=1)
_CANARY_EVENT_LEAD_TIME = timedelta(minutes=10)


class GoogleMcpCanaryError(RuntimeError):
    """A configured Google MCP server cannot complete its operational check."""


@dataclass(frozen=True)
class _GoogleCalendarArtifact:
    server_name: str
    calendar_id: str
    event_id: str


@dataclass(frozen=True)
class _GoogleDriveArtifact:
    server_name: str
    file_id: str


@dataclass(frozen=True)
class GoogleCanaryConfig:
    """Google assurance targets resolved by application composition."""

    calendar_server: str
    calendar_id: str
    drive_server: str
    drive_probe_query: str
    drive_file_id: str


@runtime_checkable
class _McpCanaryProtocol(Protocol):
    """The narrow MCP provider contract needed by Google canaries."""

    async def list_tool_names(self) -> set[str]: ...

    async def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, Any]: ...


type McpClientContextFactory = Callable[[str], AbstractAsyncContextManager[_McpCanaryProtocol]]


class GoogleCalendarRoundTripCanary:
    """Exercise Google Calendar through its managed MCP server, not direct OAuth."""

    def __init__(
        self,
        server_name: str,
        calendar_id: str,
        *,
        client_context: McpClientContextFactory | None = None,
    ) -> None:
        self._server_name = server_name
        self._calendar_id = calendar_id
        self._client_context = client_context or _managed_mcp_client

    async def exercise(self, context: CanaryRunContext) -> CanaryExercise:
        event_id = _google_event_id(context.run_id)
        start = datetime.now(UTC) + _CANARY_EVENT_LEAD_TIME
        end = start + _CANARY_EVENT_DURATION
        async with self._client_context(self._server_name) as client:
            await _require_mcp_tools(
                client,
                {"list-calendars", "list-events", "create-event", "get-event", "delete-event"},
            )
            await client.call_tool("list-calendars", {})
            await client.call_tool(
                "list-events",
                {
                    "calendarId": self._calendar_id,
                    "timeMin": (start - timedelta(minutes=1)).isoformat(),
                    "timeMax": (end + timedelta(minutes=1)).isoformat(),
                },
            )
            await client.call_tool(
                "create-event",
                {
                    "calendarId": self._calendar_id,
                    "eventId": event_id,
                    "summary": f"Pynchy canary {context.run_id}",
                    "description": "Automated Pynchy canary; removed after verification.",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "timeZone": "UTC",
                    "sendUpdates": "none",
                    "transparency": "transparent",
                    "visibility": "private",
                },
            )
        return CanaryExercise(
            artifact=_GoogleCalendarArtifact(self._server_name, self._calendar_id, event_id),
            evidence_refs=(
                "google-calendar:calendars:listed",
                _ref("google-calendar:event:created", event_id),
            ),
        )

    async def verify(self, _context: CanaryRunContext, exercise: CanaryExercise) -> tuple[str, ...]:
        artifact = _calendar_artifact(exercise)
        async with self._client_context(artifact.server_name) as client:
            await client.call_tool(
                "get-event",
                {"calendarId": artifact.calendar_id, "eventId": artifact.event_id},
            )
        return (_ref("google-calendar:event:read", artifact.event_id),)

    async def cleanup(
        self, _context: CanaryRunContext, exercise: CanaryExercise
    ) -> tuple[str, ...]:
        artifact = _calendar_artifact(exercise)
        async with self._client_context(artifact.server_name) as client:
            await client.call_tool(
                "delete-event",
                {
                    "calendarId": artifact.calendar_id,
                    "eventId": artifact.event_id,
                    "sendUpdates": "none",
                },
            )
            try:
                await client.call_tool(
                    "get-event",
                    {"calendarId": artifact.calendar_id, "eventId": artifact.event_id},
                )
            except McpCanaryToolError:
                return (_ref("google-calendar:event:deleted", artifact.event_id),)
        raise GoogleMcpCanaryError("Google Calendar retained the deleted canary event")


class GoogleDriveRoundTripCanary:
    """Exercise configured Drive search and read capabilities through managed MCP."""

    def __init__(
        self,
        server_name: str,
        probe_query: str,
        file_id: str,
        *,
        client_context: McpClientContextFactory | None = None,
    ) -> None:
        self._server_name = server_name
        self._probe_query = probe_query
        self._file_id = file_id
        self._client_context = client_context or _managed_mcp_client

    async def exercise(self, _context: CanaryRunContext) -> CanaryExercise:
        async with self._client_context(self._server_name) as client:
            await _require_mcp_tools(client, {"gdrive_search", "gdrive_read_file"})
            await client.call_tool(
                "gdrive_search",
                {"query": self._probe_query, "pageSize": 1},
            )
            await client.call_tool("gdrive_read_file", {"fileId": self._file_id})
        return CanaryExercise(
            artifact=_GoogleDriveArtifact(self._server_name, self._file_id),
            evidence_refs=(
                "google-drive:search:completed",
                _ref("google-drive:file:read", self._file_id),
            ),
        )

    async def verify(self, _context: CanaryRunContext, exercise: CanaryExercise) -> tuple[str, ...]:
        artifact = _drive_artifact(exercise)
        async with self._client_context(artifact.server_name) as client:
            await client.call_tool("gdrive_read_file", {"fileId": artifact.file_id})
        return (_ref("google-drive:file:verified", artifact.file_id),)

    async def cleanup(
        self, _context: CanaryRunContext, _exercise: CanaryExercise
    ) -> tuple[str, ...]:
        """Drive is intentionally read-only, so a successful probe creates nothing."""
        return ()


def register_google_canary_scenarios(
    register: CanaryRegistration,
    *,
    config: GoogleCanaryConfig,
) -> None:
    """Register Google canaries with configuration already resolved at composition."""
    register(
        "calendar.google.round.trip",
        GoogleCalendarRoundTripCanary(config.calendar_server, config.calendar_id),
    )
    register(
        "drive.google.round.trip",
        GoogleDriveRoundTripCanary(
            config.drive_server,
            config.drive_probe_query,
            config.drive_file_id,
        ),
    )


@asynccontextmanager
async def _managed_mcp_client(server_name: str) -> AsyncIterator[McpCanaryClient]:
    """Connect a canary to the same local MCP container that agents receive."""
    manager = get_mcp_manager()
    if manager is None:
        raise GoogleMcpCanaryError("MCP manager is not available to the canary runner")
    try:
        endpoint = await manager.get_canary_server_endpoint(server_name)
    except (RuntimeError, TimeoutError) as exc:
        raise GoogleMcpCanaryError(
            "Configured MCP server is not available to the canary runner"
        ) from exc
    async with McpCanaryClient(endpoint) as client:
        yield client


async def _require_mcp_tools(client: _McpCanaryProtocol, required: set[str]) -> None:
    if required - await client.list_tool_names():
        raise GoogleMcpCanaryError("MCP server is missing required operational tools")


def _calendar_artifact(exercise: CanaryExercise) -> _GoogleCalendarArtifact:
    if not isinstance(exercise.artifact, _GoogleCalendarArtifact):
        raise GoogleMcpCanaryError("Google Calendar canary artifact has an unexpected type")
    return exercise.artifact


def _drive_artifact(exercise: CanaryExercise) -> _GoogleDriveArtifact:
    if not isinstance(exercise.artifact, _GoogleDriveArtifact):
        raise GoogleMcpCanaryError("Google Drive canary artifact has an unexpected type")
    return exercise.artifact


def _google_event_id(run_id: str) -> str:
    """Make an allowed Google event ID without storing a provider response."""
    return f"pnc{hashlib.sha256(run_id.encode()).hexdigest()[:26]}"


def _ref(prefix: str, value: str) -> str:
    return f"{prefix}:{hashlib.sha256(value.encode()).hexdigest()[:12]}"
