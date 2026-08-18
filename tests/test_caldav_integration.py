"""Tests for CalDAV calendar integration via the MCP server plugin."""

from __future__ import annotations

# allow: file-length -- CalDAV service contracts share one provider fixture set.
import json
import os
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from conftest import NullIpcDeps

from pynchy.config.api import CalDAVConfig, CalDAVServerConfig, CalDAVTool
from pynchy.host.container_manager.ipc.handlers_service import clear_plugin_handler_cache
from pynchy.host.container_manager.ipc.registry import dispatch
from pynchy.host.container_manager.security.gate import create_gate, destroy_gate
from pynchy.plugins.integrations.caldav import (
    CalDAVMcpServerPlugin,
    CalDAVRuntime,
    CalDAVServerOptions,
    clear_caldav_client_cache,
    configure_caldav_runtime,
)
from pynchy.state import init_test_database
from pynchy.workspace.api import (
    CapabilityRule,
    ServiceTrustConfig,
    WorkspaceProfile,
    WorkspaceSecurity,
)

# CalDAV service handlers are exposed through the plugin's public tool contract —
# the same registry the IPC service dispatcher consumes. Resolve them here rather
# than importing the private handler functions, so tests drive the public surface.
_CALDAV_REGISTRATION = CalDAVMcpServerPlugin().pynchy_service_handler()


def _caldav_handler(tool_name: str):
    action = _CALDAV_REGISTRATION.action_for(tool_name)
    assert action is not None
    return action.handler


_handle_list_calendars = _caldav_handler("list_calendars")
_handle_list_calendar = _caldav_handler("list_calendar")
_handle_create_event = _caldav_handler("create_event")
_handle_delete_event = _caldav_handler("delete_event")


@pytest.fixture(autouse=True)
async def _setup():
    await init_test_database()
    clear_caldav_client_cache()
    clear_plugin_handler_cache()
    yield
    # The only SecurityGate this module registers is the e2e test's fixed key;
    # drop it via the public API so gate state never leaks across tests.
    destroy_gate("test-ws", 1000.0)


class FakeDeps(NullIpcDeps):
    def __init__(self, groups=None):
        self._groups = groups or {}

    def workspaces(self):
        return self._groups


@dataclass(frozen=True)
class _WorkspaceSettings:
    """The workspace-config subset consulted by the CalDAV service adapter."""

    security: WorkspaceSecurity


TEST_GROUP = WorkspaceProfile(
    jid="test@g.us",
    name="Test",
    folder="test-ws",
    trigger="@Pynchy",
    added_at="2024-01-01",
)

os.environ.setdefault("CALDAV_TEST_WORK_PASS", "workpass")  # pragma: allowlist secret
os.environ.setdefault("CALDAV_TEST_PERSONAL_PASS", "personalpass")  # pragma: allowlist secret

WORK_ENV_VAR = "CALDAV_TEST_WORK_PASS"  # pragma: allowlist secret
PERSONAL_ENV_VAR = "CALDAV_TEST_PERSONAL_PASS"  # pragma: allowlist secret

WORK_SERVER = CalDAVServerConfig(
    url="https://work.nextcloud.com/remote.php/dav/",
    username="user@work.com",
    password_env=WORK_ENV_VAR,
    default_calendar="meetings",
)

PERSONAL_SERVER = CalDAVServerConfig(
    url="https://personal.nextcloud.com/remote.php/dav/",
    username="me@example.com",
    password_env=PERSONAL_ENV_VAR,
)

CALDAV_CONFIG = CalDAVConfig(
    default_server="work",
    servers={"work": WORK_SERVER, "personal": PERSONAL_SERVER},
)

EMPTY_CALDAV_CONFIG = CalDAVConfig()


def _runtime_servers(config: CalDAVConfig) -> dict[str, CalDAVServerOptions]:
    return {
        name: CalDAVServerOptions(
            url=server.url,
            username=server.username,
            password_env=server.password_env,
            default_calendar=server.default_calendar,
            allow=tuple(server.allow) if server.allow is not None else None,
            ignore=tuple(server.ignore) if server.ignore is not None else None,
        )
        for name, server in config.servers.items()
    }


def _make_settings(caldav_cfg=CALDAV_CONFIG, ws_security=None):
    """Create fake settings with CalDAV and workspace security configured."""

    configure_caldav_runtime(
        CalDAVRuntime(
            default_server=caldav_cfg.default_server,
            servers=_runtime_servers(caldav_cfg),
        )
    )

    class FakeSettings:
        def __init__(self):
            self.tools = {
                "caldav": CalDAVTool(
                    type="caldav",
                    default_server=caldav_cfg.default_server,
                    servers=caldav_cfg.servers,
                )
            }
            self.workspaces = {
                "test-ws": _WorkspaceSettings(security=ws_security or WorkspaceSecurity()),
            }

    return FakeSettings()


def _make_fake_event(
    uid="event-123", summary="Test Event", dtstart=None, dtend=None, description=None, location=None
):
    """Create a fake caldav event with icalendar_component."""
    if dtstart is None:
        dtstart = datetime(2026, 2, 16, 10, 0, tzinfo=UTC)
    if dtend is None:
        dtend = datetime(2026, 2, 16, 11, 0, tzinfo=UTC)

    component = MagicMock()

    def fake_get(key):
        values = {
            "uid": uid,
            "summary": summary,
            "dtstart": MagicMock(dt=dtstart),
            "dtend": MagicMock(dt=dtend),
            "description": description,
            "location": location,
        }
        return values.get(key)

    component.get = fake_get

    event = MagicMock()
    event.icalendar_component = component
    return event


def _make_fake_cal(name):
    """Create a fake CalDAV calendar object."""
    cal = MagicMock()
    cal.name = name
    return cal


def _make_fake_client(*calendar_names):
    """Create a fake CalDAV client with given calendar names."""
    cals = [_make_fake_cal(n) for n in calendar_names]
    fake_principal = MagicMock()
    fake_principal.calendars.return_value = cals
    fake_client = MagicMock()
    fake_client.principal.return_value = fake_principal
    return fake_client, cals


# ---------------------------------------------------------------------------
# Not-configured tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_calendar_not_configured():
    """Returns error when no CalDAV servers are configured."""
    settings = _make_settings(caldav_cfg=EMPTY_CALDAV_CONFIG)
    with nullcontext():
        result = await _handle_list_calendar({"calendar": "primary"})
    assert "error" in result
    assert "not configured" in result["error"].lower()


@pytest.mark.asyncio
async def test_create_event_not_configured():
    settings = _make_settings(caldav_cfg=EMPTY_CALDAV_CONFIG)
    with nullcontext():
        result = await _handle_create_event(
            {
                "title": "Test",
                "start": "2026-02-16T10:00:00",
                "end": "2026-02-16T11:00:00",
            }
        )
    assert "error" in result
    assert "not configured" in result["error"].lower()


@pytest.mark.asyncio
async def test_delete_event_not_configured():
    settings = _make_settings(caldav_cfg=EMPTY_CALDAV_CONFIG)
    with nullcontext():
        result = await _handle_delete_event({"event_id": "uid-123", "calendar": "primary"})
    assert "error" in result
    assert "not configured" in result["error"].lower()


@pytest.mark.asyncio
async def test_list_calendars_not_configured():
    settings = _make_settings(caldav_cfg=EMPTY_CALDAV_CONFIG)
    with nullcontext():
        result = await _handle_list_calendars({})
    assert "error" in result
    assert "not configured" in result["error"].lower()


# ---------------------------------------------------------------------------
# Server / calendar resolution tests (driven through the public list handlers)
# ---------------------------------------------------------------------------

_NO_CALENDAR = object()


def _single_server_cfg(*, allow=None, ignore=None):
    """A one-server CalDAV config for exercising allow/ignore filtering."""
    return CalDAVConfig(
        default_server="work",
        servers={
            "work": CalDAVServerConfig(
                url="http://x",
                username="u",
                password_env=WORK_ENV_VAR,
                allow=allow,
                ignore=ignore,
            ),
        },
    )


async def _resolve_via_list(cfg, calendar_arg, *calendar_names):
    """Drive the public list_calendar handler and report how it resolved.

    Returns ``(server_requested, searched_calendar, result)`` — the server whose
    client was requested and the calendar that was actually queried are the
    observable effects of ``_resolve_server``/``_resolve_calendar``.
    """
    fake_client, cals = _make_fake_client(*calendar_names)
    for cal in cals:
        cal.date_search.return_value = []
    settings = _make_settings(caldav_cfg=cfg)
    get_client = MagicMock(return_value=fake_client)
    data = {} if calendar_arg is _NO_CALENDAR else {"calendar": calendar_arg}
    with (
        patch("pynchy.plugins.integrations.caldav.get_caldav_client", get_client),
    ):
        result = await _handle_list_calendar(data)
    server_requested = get_client.call_args.args[0] if get_client.call_args else None
    searched = next((c.name for c in cals if c.date_search.called), None)
    return server_requested, searched, result


async def _visible_calendars(cfg, *calendar_names):
    """Return the visible calendars the public list_calendars handler reports for
    the 'work' server — the observable result of allow/ignore filtering."""
    fake_client, _ = _make_fake_client(*calendar_names)
    settings = _make_settings(caldav_cfg=cfg)
    with (
        patch("pynchy.plugins.integrations.caldav.get_caldav_client", return_value=fake_client),
    ):
        result = await _handle_list_calendars({})
    return result["result"]["servers"]["work"]


@pytest.mark.asyncio
async def test_resolve_server_explicit():
    """'work/meetings' drives the work server and its 'meetings' calendar."""
    server, searched, result = await _resolve_via_list(CALDAV_CONFIG, "work/meetings", "meetings")
    assert server == "work"
    assert searched == "meetings"
    assert "result" in result


@pytest.mark.asyncio
async def test_resolve_server_default():
    """'meetings' (no prefix) drives the default server (work)."""
    server, searched, result = await _resolve_via_list(CALDAV_CONFIG, "meetings", "meetings")
    assert server == "work"
    assert searched == "meetings"
    assert "result" in result


@pytest.mark.asyncio
async def test_resolve_server_primary():
    """'primary' resolves to the default server's default_calendar (meetings)."""
    server, searched, result = await _resolve_via_list(
        CALDAV_CONFIG, "primary", "meetings", "standup"
    )
    assert server == "work"
    assert searched == "meetings"  # work's default_calendar wins over 'standup'
    assert "result" in result


@pytest.mark.asyncio
async def test_resolve_server_primary_no_default_calendar():
    """'personal/primary' (no default_calendar) uses the first visible calendar."""
    server, searched, result = await _resolve_via_list(
        CALDAV_CONFIG, "personal/primary", "cal-a", "cal-b"
    )
    assert server == "personal"
    assert searched == "cal-a"  # first visible — personal has no default_calendar
    assert "result" in result


@pytest.mark.asyncio
async def test_resolve_server_none_defaults_to_primary():
    """A missing calendar argument defaults to the default server + calendar."""
    server, searched, result = await _resolve_via_list(
        CALDAV_CONFIG, _NO_CALENDAR, "meetings", "standup"
    )
    assert server == "work"
    assert searched == "meetings"
    assert "result" in result


@pytest.mark.asyncio
async def test_resolve_server_unknown():
    """An unknown server name surfaces a 'not found' error."""
    _server, _searched, result = await _resolve_via_list(CALDAV_CONFIG, "nonexistent/cal")
    assert "error" in result
    assert "not found" in result["error"].lower()


@pytest.mark.asyncio
async def test_resolve_server_empty_default():
    """An empty default_server with no prefix surfaces a 'not found' error."""
    cfg = CalDAVConfig(default_server="", servers={"work": WORK_SERVER})
    _server, _searched, result = await _resolve_via_list(cfg, "meetings")
    assert "error" in result
    assert "not found" in result["error"].lower()


# ---------------------------------------------------------------------------
# Allow/ignore filtering tests (driven through the public list_calendars handler)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_visible_no_filters():
    """All calendars are visible when no allow/ignore set."""
    visible = await _visible_calendars(_single_server_cfg(), "anything")
    assert visible == ["anything"]


@pytest.mark.asyncio
async def test_is_visible_allow_match():
    """Calendars in the allow list are visible (case-insensitively)."""
    visible = await _visible_calendars(
        _single_server_cfg(allow=["meetings", "personal"]), "meetings", "Meetings"
    )
    assert "meetings" in visible
    assert "Meetings" in visible  # case-insensitive match


@pytest.mark.asyncio
async def test_is_visible_allow_no_match():
    """Calendars absent from the allow list are hidden."""
    visible = await _visible_calendars(_single_server_cfg(allow=["meetings"]), "trash")
    assert "trash" not in visible


@pytest.mark.asyncio
async def test_is_visible_ignore_match():
    """Calendars in the ignore list are hidden (case-insensitively)."""
    visible = await _visible_calendars(
        _single_server_cfg(ignore=["trash", "birthdays"]), "trash", "Trash"
    )
    assert "trash" not in visible
    assert "Trash" not in visible


@pytest.mark.asyncio
async def test_is_visible_ignore_no_match():
    """Calendars absent from the ignore list stay visible."""
    visible = await _visible_calendars(_single_server_cfg(ignore=["trash"]), "meetings")
    assert "meetings" in visible


@pytest.mark.asyncio
async def test_allow_overrides_ignore():
    """When both allow and ignore list a calendar, allow wins."""
    visible = await _visible_calendars(
        _single_server_cfg(allow=["meetings"], ignore=["meetings"]), "meetings", "other"
    )
    assert "meetings" in visible  # allow wins
    assert "other" not in visible  # allow list is exclusive


# ---------------------------------------------------------------------------
# list_calendar tests
# ---------------------------------------------------------------------------


@pytest.mark.action("calendar.event.list")
@pytest.mark.asyncio
async def test_list_calendar_returns_events():
    """list_calendar returns parsed events from CalDAV."""
    fake_event = _make_fake_event(
        uid="ev-1",
        summary="Meeting",
        dtstart=datetime(2026, 2, 16, 14, 0, tzinfo=UTC),
        dtend=datetime(2026, 2, 16, 15, 0, tzinfo=UTC),
        description="Weekly sync",
        location="Room A",
    )

    fake_client, cals = _make_fake_client("meetings", "standup")
    cals[0].date_search.return_value = [fake_event]

    settings = _make_settings()

    with (
        patch("pynchy.plugins.integrations.caldav.get_caldav_client", return_value=fake_client),
    ):
        result = await _handle_list_calendar(
            {
                "start_date": "2026-02-16T00:00:00+00:00",
                "end_date": "2026-02-17T00:00:00+00:00",
                "calendar": "primary",
            }
        )

    assert "result" in result
    assert result["result"]["count"] == 1
    event = result["result"]["events"][0]
    assert event["uid"] == "ev-1"
    assert event["title"] == "Meeting"
    assert event["description"] == "Weekly sync"
    assert event["location"] == "Room A"


@pytest.mark.asyncio
async def test_list_calendar_skips_results_without_icalendar_components():
    empty_event = MagicMock(icalendar_component=None)
    fake_client, cals = _make_fake_client("meetings")
    cals[0].date_search.return_value = [empty_event]

    with patch("pynchy.plugins.integrations.caldav.get_caldav_client", return_value=fake_client):
        result = await _handle_list_calendar({"calendar": "primary"})

    assert result["result"] == {"events": [], "count": 0}


@pytest.mark.asyncio
async def test_list_calendar_defaults_to_7_days():
    """list_calendar uses 7-day range when no dates provided."""
    fake_client, cals = _make_fake_client("meetings")
    cals[0].date_search.return_value = []

    settings = _make_settings()

    with (
        patch("pynchy.plugins.integrations.caldav.get_caldav_client", return_value=fake_client),
    ):
        result = await _handle_list_calendar({"calendar": "primary"})

    assert "result" in result
    assert result["result"]["count"] == 0

    # Verify date_search was called with a ~7 day range
    call_args = cals[0].date_search.call_args
    start = call_args.kwargs["start"]
    end = call_args.kwargs["end"]
    diff = end - start
    assert 6 <= diff.days <= 7


@pytest.mark.asyncio
async def test_list_calendar_explicit_server():
    """'personal/my-cal' resolves to the personal server."""
    fake_client, cals = _make_fake_client("my-cal")
    cals[0].date_search.return_value = []

    settings = _make_settings()

    with (
        patch("pynchy.plugins.integrations.caldav.get_caldav_client", return_value=fake_client),
    ):
        result = await _handle_list_calendar({"calendar": "personal/my-cal"})

    assert "result" in result


@pytest.mark.asyncio
async def test_list_calendar_not_found():
    """Error when requested calendar doesn't exist."""
    fake_client, _ = _make_fake_client("other-cal")

    settings = _make_settings()

    with (
        patch("pynchy.plugins.integrations.caldav.get_caldav_client", return_value=fake_client),
    ):
        result = await _handle_list_calendar({"calendar": "nonexistent"})

    assert "error" in result
    assert "not found" in result["error"].lower()


@pytest.mark.asyncio
async def test_list_calendar_filtered_out():
    """Calendar hidden by ignore list returns error."""
    cfg = CalDAVConfig(
        default_server="work",
        servers={
            "work": CalDAVServerConfig(
                url="http://x",
                username="u",
                password_env=WORK_ENV_VAR,
                ignore=["secret-cal"],
            ),
        },
    )
    fake_client, _ = _make_fake_client("meetings", "secret-cal")

    settings = _make_settings(caldav_cfg=cfg)

    with (
        patch("pynchy.plugins.integrations.caldav.get_caldav_client", return_value=fake_client),
    ):
        result = await _handle_list_calendar({"calendar": "secret-cal"})

    assert "error" in result
    assert "not found" in result["error"].lower()


# ---------------------------------------------------------------------------
# list_calendars tests
# ---------------------------------------------------------------------------


@pytest.mark.action("calendar.calendar.list")
@pytest.mark.asyncio
async def test_list_calendars_discovers_all():
    """list_calendars returns calendars from all configured servers."""
    fake_client, _ = _make_fake_client("meetings", "standup", "personal")

    settings = _make_settings()

    with (
        patch("pynchy.plugins.integrations.caldav.get_caldav_client", return_value=fake_client),
    ):
        result = await _handle_list_calendars({})

    assert "result" in result
    assert "servers" in result["result"]
    assert result["result"]["default_server"] == "work"
    # Both servers use same fake client, so both see same calendars
    assert "work" in result["result"]["servers"]
    assert "personal" in result["result"]["servers"]
    assert "meetings" in result["result"]["servers"]["work"]


@pytest.mark.asyncio
async def test_list_calendars_respects_ignore():
    """list_calendars filters out ignored calendars."""
    cfg = CalDAVConfig(
        default_server="work",
        servers={
            "work": CalDAVServerConfig(
                url="http://x",
                username="u",
                password_env=WORK_ENV_VAR,
                ignore=["trash"],
            ),
        },
    )
    fake_client, _ = _make_fake_client("meetings", "trash", "standup")

    settings = _make_settings(caldav_cfg=cfg)

    with (
        patch("pynchy.plugins.integrations.caldav.get_caldav_client", return_value=fake_client),
    ):
        result = await _handle_list_calendars({})

    cals = result["result"]["servers"]["work"]
    assert "meetings" in cals
    assert "standup" in cals
    assert "trash" not in cals


@pytest.mark.asyncio
async def test_list_calendars_respects_allow():
    """list_calendars only shows allowed calendars."""
    cfg = CalDAVConfig(
        default_server="work",
        servers={
            "work": CalDAVServerConfig(
                url="http://x",
                username="u",
                password_env=WORK_ENV_VAR,
                allow=["meetings"],
            ),
        },
    )
    fake_client, _ = _make_fake_client("meetings", "trash", "standup")

    settings = _make_settings(caldav_cfg=cfg)

    with (
        patch("pynchy.plugins.integrations.caldav.get_caldav_client", return_value=fake_client),
    ):
        result = await _handle_list_calendars({})

    cals = result["result"]["servers"]["work"]
    assert cals == ["meetings"]


# ---------------------------------------------------------------------------
# create_event tests
# ---------------------------------------------------------------------------


@pytest.mark.action("calendar.event.create")
@pytest.mark.asyncio
async def test_create_event_success():
    """create_event calls save_event and returns UID."""
    created_event = _make_fake_event(uid="new-uid-1")

    fake_client, cals = _make_fake_client("meetings")
    cals[0].save_event.return_value = created_event

    settings = _make_settings()

    with (
        patch("pynchy.plugins.integrations.caldav.get_caldav_client", return_value=fake_client),
    ):
        result = await _handle_create_event(
            {
                "title": "New Meeting",
                "start": "2026-02-20T10:00:00+00:00",
                "end": "2026-02-20T11:00:00+00:00",
                "description": "Discuss plans",
                "location": "Office",
                "calendar": "primary",
            }
        )

    assert "result" in result
    assert result["result"]["uid"] == "new-uid-1"
    assert result["result"]["status"] == "created"

    # Verify save_event was called with correct kwargs
    call_kwargs = cals[0].save_event.call_args.kwargs
    assert call_kwargs["summary"] == "New Meeting"
    assert call_kwargs["description"] == "Discuss plans"
    assert call_kwargs["location"] == "Office"
    assert isinstance(call_kwargs["dtstart"], datetime)
    assert isinstance(call_kwargs["dtend"], datetime)


@pytest.mark.asyncio
async def test_create_event_minimal():
    """create_event works with only required fields (no description/location)."""
    created_event = _make_fake_event(uid="min-uid")
    created_event.icalendar_component = None

    fake_client, cals = _make_fake_client("meetings")
    cals[0].save_event.return_value = created_event

    settings = _make_settings()

    with (
        patch("pynchy.plugins.integrations.caldav.get_caldav_client", return_value=fake_client),
    ):
        result = await _handle_create_event(
            {
                "title": "Quick Call",
                "start": "2026-02-20T10:00:00",
                "end": "2026-02-20T10:30:00",
                "calendar": "primary",
            }
        )

    assert result["result"]["status"] == "created"
    call_kwargs = cals[0].save_event.call_args.kwargs
    assert "description" not in call_kwargs
    assert "location" not in call_kwargs


@pytest.mark.asyncio
async def test_create_event_returns_no_uid_when_provider_omits_uid():
    created_event = _make_fake_event(uid=None)
    fake_client, cals = _make_fake_client("meetings")
    cals[0].save_event.return_value = created_event

    with patch("pynchy.plugins.integrations.caldav.get_caldav_client", return_value=fake_client):
        result = await _handle_create_event(
            {
                "title": "Quick Call",
                "start": "2026-02-20T10:00:00",
                "end": "2026-02-20T10:30:00",
                "calendar": "primary",
            }
        )

    assert result["result"] == {"uid": None, "status": "created"}


@pytest.mark.asyncio
async def test_create_event_explicit_server():
    """create_event with 'personal/my-cal' targets the personal server."""
    created_event = _make_fake_event(uid="personal-uid")

    fake_client, cals = _make_fake_client("my-cal")
    cals[0].save_event.return_value = created_event

    settings = _make_settings()

    with (
        patch("pynchy.plugins.integrations.caldav.get_caldav_client", return_value=fake_client),
    ):
        result = await _handle_create_event(
            {
                "title": "Personal Event",
                "start": "2026-02-20T10:00:00",
                "end": "2026-02-20T11:00:00",
                "calendar": "personal/my-cal",
            }
        )

    assert result["result"]["status"] == "created"


# ---------------------------------------------------------------------------
# delete_event tests
# ---------------------------------------------------------------------------


@pytest.mark.action("calendar.event.delete")
@pytest.mark.asyncio
async def test_delete_event_success():
    """delete_event calls event.delete() and returns confirmation."""
    fake_event = MagicMock()

    fake_client, cals = _make_fake_client("meetings")
    cals[0].event_by_uid.return_value = fake_event

    settings = _make_settings()

    with (
        patch("pynchy.plugins.integrations.caldav.get_caldav_client", return_value=fake_client),
    ):
        result = await _handle_delete_event(
            {
                "event_id": "uid-to-delete",
                "calendar": "primary",
            }
        )

    assert "result" in result
    assert result["result"]["uid"] == "uid-to-delete"
    assert result["result"]["status"] == "deleted"
    cals[0].event_by_uid.assert_called_once_with("uid-to-delete")
    fake_event.delete.assert_called_once()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_caldav_connection_error():
    """CalDAV connection failure returns error response."""
    settings = _make_settings()

    with (
        patch(
            "pynchy.plugins.integrations.caldav.get_caldav_client",
            side_effect=Exception("Connection refused"),
        ),
    ):
        result = await _handle_list_calendar({"calendar": "primary"})

    assert "error" in result
    assert "Connection refused" in result["error"]


@pytest.mark.asyncio
async def test_unknown_server_error():
    """Requesting a nonexistent server returns error."""
    settings = _make_settings()

    with nullcontext():
        result = await _handle_list_calendar({"calendar": "nonexistent-server/cal"})

    assert "error" in result
    assert "not found" in result["error"].lower()


# ---------------------------------------------------------------------------
# End-to-end: service request dispatches to CalDAV plugin handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calendar_tool_dispatches_to_plugin_handler(tmp_path):
    """Calendar service requests go through policy and dispatch to CalDAV plugin handler."""
    fake_event = _make_fake_event(uid="e2e-1", summary="E2E Test")

    fake_client, cals = _make_fake_client("meetings")
    cals[0].date_search.return_value = [fake_event]

    # Register a SecurityGate with all-safe trust for list_calendar
    security = WorkspaceSecurity(
        capabilities={"*": CapabilityRule("allow")},
        services={
            "list_calendar": ServiceTrustConfig(
                public_source=False,
                secret_data=False,
                public_sink=False,
                dangerous_writes=False,
            ),
        },
    )
    create_gate("test-ws", 1000.0, security)

    settings = _make_settings()
    settings.data_dir = tmp_path

    deps = FakeDeps({"test@g.us": TEST_GROUP})

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_service.get_settings", return_value=settings
        ),
        patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"),
        patch("pynchy.plugins.integrations.caldav.get_caldav_client", return_value=fake_client),
    ):
        data = {
            "type": "service:list_calendar",
            "request_id": "cal-req-1",
            "start_date": "2026-02-16T00:00:00+00:00",
            "end_date": "2026-02-17T00:00:00+00:00",
            "calendar": "primary",
        }
        await dispatch(data, "test-ws", False, deps)

    response_file = tmp_path / "ipc" / "test-ws" / "responses" / "cal-req-1.json"
    assert response_file.exists()
    response = json.loads(response_file.read_text())
    assert "result" in response
    assert response["result"]["count"] == 1
    assert response["result"]["events"][0]["uid"] == "e2e-1"
