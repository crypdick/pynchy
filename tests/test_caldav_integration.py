"""Tests for CalDAV calendar integration via the MCP server plugin."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from pynchy.config import (
    CalDAVConfig,
    CalDAVServerConfig,
    WorkspaceConfig,
    WorkspaceSecurityConfig,
)
from pynchy.db import _init_test_database
from pynchy.integrations.plugins.caldav import (
    _handle_create_event,
    _handle_delete_event,
    _handle_list_calendar,
    _handle_list_calendars,
    _is_calendar_visible,
    _resolve_server,
    clear_caldav_client_cache,
)
from pynchy.ipc._handlers_service import (
    _handle_service_request,
    clear_plugin_handler_cache,
    clear_policy_cache,
)
from pynchy.types import WorkspaceProfile


@pytest.fixture(autouse=True)
async def _setup():
    await _init_test_database()
    clear_policy_cache()
    clear_caldav_client_cache()
    clear_plugin_handler_cache()


class FakeDeps:
    def __init__(self, groups=None):
        self._groups = groups or {}

    def workspaces(self):
        return self._groups


TEST_GROUP = WorkspaceProfile(
    jid="test@g.us",
    name="Test",
    folder="test-ws",
    trigger="@Pynchy",
    added_at="2024-01-01",
)

WORK_SERVER = CalDAVServerConfig(
    url="https://work.nextcloud.com/remote.php/dav/",
    username="user@work.com",
    password="workpass",  # pragma: allowlist secret
    default_calendar="meetings",
)

PERSONAL_SERVER = CalDAVServerConfig(
    url="https://personal.nextcloud.com/remote.php/dav/",
    username="me@example.com",
    password="personalpass",  # pragma: allowlist secret
)

CALDAV_CONFIG = CalDAVConfig(
    default_server="work",
    servers={"work": WORK_SERVER, "personal": PERSONAL_SERVER},
)

EMPTY_CALDAV_CONFIG = CalDAVConfig()


def _make_settings(caldav_cfg=CALDAV_CONFIG, ws_security=None):
    """Create fake settings with CalDAV and workspace security configured."""

    class FakeSettings:
        def __init__(self):
            self.caldav = caldav_cfg
            self.workspaces = {
                "test-ws": WorkspaceConfig(
                    name="test",
                    security=ws_security
                    or WorkspaceSecurityConfig(
                        default_risk_tier="always-approve",
                    ),
                ),
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
    with patch("pynchy.integrations.plugins.caldav.get_settings", return_value=settings):
        result = await _handle_list_calendar({"calendar": "primary"})
    assert "error" in result
    assert "not configured" in result["error"].lower()


@pytest.mark.asyncio
async def test_create_event_not_configured():
    settings = _make_settings(caldav_cfg=EMPTY_CALDAV_CONFIG)
    with patch("pynchy.integrations.plugins.caldav.get_settings", return_value=settings):
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
    with patch("pynchy.integrations.plugins.caldav.get_settings", return_value=settings):
        result = await _handle_delete_event({"event_id": "uid-123", "calendar": "primary"})
    assert "error" in result
    assert "not configured" in result["error"].lower()


@pytest.mark.asyncio
async def test_list_calendars_not_configured():
    settings = _make_settings(caldav_cfg=EMPTY_CALDAV_CONFIG)
    with patch("pynchy.integrations.plugins.caldav.get_settings", return_value=settings):
        result = await _handle_list_calendars({})
    assert "error" in result
    assert "not configured" in result["error"].lower()


# ---------------------------------------------------------------------------
# Server resolution tests
# ---------------------------------------------------------------------------


def test_resolve_server_explicit():
    """'work/meetings' resolves to work server with calendar 'meetings'."""
    name, cfg, cal = _resolve_server(CALDAV_CONFIG, "work/meetings")
    assert name == "work"
    assert cfg is WORK_SERVER
    assert cal == "meetings"


def test_resolve_server_default():
    """'meetings' resolves to default server (work)."""
    name, cfg, cal = _resolve_server(CALDAV_CONFIG, "meetings")
    assert name == "work"
    assert cfg is WORK_SERVER
    assert cal == "meetings"


def test_resolve_server_primary():
    """'primary' resolves to default server's default_calendar."""
    name, cfg, cal = _resolve_server(CALDAV_CONFIG, "primary")
    assert name == "work"
    assert cfg is WORK_SERVER
    assert cal == "meetings"  # work server's default_calendar


def test_resolve_server_primary_no_default_calendar():
    """'primary' with no default_calendar returns None (first-visible)."""
    name, cfg, cal = _resolve_server(CALDAV_CONFIG, "personal/primary")
    assert name == "personal"
    assert cfg is PERSONAL_SERVER
    assert cal is None  # personal has no default_calendar


def test_resolve_server_none_defaults_to_primary():
    """None calendar string defaults to 'primary'."""
    name, cfg, cal = _resolve_server(CALDAV_CONFIG, None)
    assert name == "work"
    assert cal == "meetings"


def test_resolve_server_unknown():
    """Unknown server name raises ValueError."""
    with pytest.raises(ValueError, match="not found"):
        _resolve_server(CALDAV_CONFIG, "nonexistent/cal")


def test_resolve_server_empty_default():
    """Empty default_server and no prefix raises ValueError."""
    cfg = CalDAVConfig(default_server="", servers={"work": WORK_SERVER})
    with pytest.raises(ValueError, match="not found"):
        _resolve_server(cfg, "meetings")


# ---------------------------------------------------------------------------
# Allow/ignore filtering tests
# ---------------------------------------------------------------------------


def test_is_visible_no_filters():
    """All calendars visible when no allow/ignore set."""
    cfg = CalDAVServerConfig(url="http://x", username="u")
    assert _is_calendar_visible("anything", cfg) is True


def test_is_visible_allow_match():
    """Calendar in allow list is visible."""
    cfg = CalDAVServerConfig(url="http://x", username="u", allow=["meetings", "personal"])
    assert _is_calendar_visible("meetings", cfg) is True
    assert _is_calendar_visible("Meetings", cfg) is True  # case-insensitive


def test_is_visible_allow_no_match():
    """Calendar not in allow list is hidden."""
    cfg = CalDAVServerConfig(url="http://x", username="u", allow=["meetings"])
    assert _is_calendar_visible("trash", cfg) is False


def test_is_visible_ignore_match():
    """Calendar in ignore list is hidden."""
    cfg = CalDAVServerConfig(url="http://x", username="u", ignore=["trash", "birthdays"])
    assert _is_calendar_visible("trash", cfg) is False
    assert _is_calendar_visible("Trash", cfg) is False  # case-insensitive


def test_is_visible_ignore_no_match():
    """Calendar not in ignore list is visible."""
    cfg = CalDAVServerConfig(url="http://x", username="u", ignore=["trash"])
    assert _is_calendar_visible("meetings", cfg) is True


def test_allow_overrides_ignore():
    """When both allow and ignore are set, allow wins."""
    cfg = CalDAVServerConfig(url="http://x", username="u", allow=["meetings"], ignore=["meetings"])
    # "meetings" is in both — allow wins, so it's visible
    assert _is_calendar_visible("meetings", cfg) is True
    # "other" is not in allow, so it's hidden (allow list is exclusive)
    assert _is_calendar_visible("other", cfg) is False


# ---------------------------------------------------------------------------
# list_calendar tests
# ---------------------------------------------------------------------------


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
        patch("pynchy.integrations.plugins.caldav.get_settings", return_value=settings),
        patch("pynchy.integrations.plugins.caldav._get_caldav_client", return_value=fake_client),
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
async def test_list_calendar_defaults_to_7_days():
    """list_calendar uses 7-day range when no dates provided."""
    fake_client, cals = _make_fake_client("meetings")
    cals[0].date_search.return_value = []

    settings = _make_settings()

    with (
        patch("pynchy.integrations.plugins.caldav.get_settings", return_value=settings),
        patch("pynchy.integrations.plugins.caldav._get_caldav_client", return_value=fake_client),
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
        patch("pynchy.integrations.plugins.caldav.get_settings", return_value=settings),
        patch("pynchy.integrations.plugins.caldav._get_caldav_client", return_value=fake_client),
    ):
        result = await _handle_list_calendar({"calendar": "personal/my-cal"})

    assert "result" in result


@pytest.mark.asyncio
async def test_list_calendar_not_found():
    """Error when requested calendar doesn't exist."""
    fake_client, _ = _make_fake_client("other-cal")

    settings = _make_settings()

    with (
        patch("pynchy.integrations.plugins.caldav.get_settings", return_value=settings),
        patch("pynchy.integrations.plugins.caldav._get_caldav_client", return_value=fake_client),
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
                password="p",
                ignore=["secret-cal"],
            ),
        },
    )
    fake_client, _ = _make_fake_client("meetings", "secret-cal")

    settings = _make_settings(caldav_cfg=cfg)

    with (
        patch("pynchy.integrations.plugins.caldav.get_settings", return_value=settings),
        patch("pynchy.integrations.plugins.caldav._get_caldav_client", return_value=fake_client),
    ):
        result = await _handle_list_calendar({"calendar": "secret-cal"})

    assert "error" in result
    assert "not found" in result["error"].lower()


# ---------------------------------------------------------------------------
# list_calendars tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_calendars_discovers_all():
    """list_calendars returns calendars from all configured servers."""
    fake_client, _ = _make_fake_client("meetings", "standup", "personal")

    settings = _make_settings()

    with (
        patch("pynchy.integrations.plugins.caldav.get_settings", return_value=settings),
        patch("pynchy.integrations.plugins.caldav._get_caldav_client", return_value=fake_client),
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
                password="p",
                ignore=["trash"],
            ),
        },
    )
    fake_client, _ = _make_fake_client("meetings", "trash", "standup")

    settings = _make_settings(caldav_cfg=cfg)

    with (
        patch("pynchy.integrations.plugins.caldav.get_settings", return_value=settings),
        patch("pynchy.integrations.plugins.caldav._get_caldav_client", return_value=fake_client),
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
                password="p",
                allow=["meetings"],
            ),
        },
    )
    fake_client, _ = _make_fake_client("meetings", "trash", "standup")

    settings = _make_settings(caldav_cfg=cfg)

    with (
        patch("pynchy.integrations.plugins.caldav.get_settings", return_value=settings),
        patch("pynchy.integrations.plugins.caldav._get_caldav_client", return_value=fake_client),
    ):
        result = await _handle_list_calendars({})

    cals = result["result"]["servers"]["work"]
    assert cals == ["meetings"]


# ---------------------------------------------------------------------------
# create_event tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_event_success():
    """create_event calls save_event and returns UID."""
    created_event = _make_fake_event(uid="new-uid-1")

    fake_client, cals = _make_fake_client("meetings")
    cals[0].save_event.return_value = created_event

    settings = _make_settings()

    with (
        patch("pynchy.integrations.plugins.caldav.get_settings", return_value=settings),
        patch("pynchy.integrations.plugins.caldav._get_caldav_client", return_value=fake_client),
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

    fake_client, cals = _make_fake_client("meetings")
    cals[0].save_event.return_value = created_event

    settings = _make_settings()

    with (
        patch("pynchy.integrations.plugins.caldav.get_settings", return_value=settings),
        patch("pynchy.integrations.plugins.caldav._get_caldav_client", return_value=fake_client),
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
async def test_create_event_explicit_server():
    """create_event with 'personal/my-cal' targets the personal server."""
    created_event = _make_fake_event(uid="personal-uid")

    fake_client, cals = _make_fake_client("my-cal")
    cals[0].save_event.return_value = created_event

    settings = _make_settings()

    with (
        patch("pynchy.integrations.plugins.caldav.get_settings", return_value=settings),
        patch("pynchy.integrations.plugins.caldav._get_caldav_client", return_value=fake_client),
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


@pytest.mark.asyncio
async def test_delete_event_success():
    """delete_event calls event.delete() and returns confirmation."""
    fake_event = MagicMock()

    fake_client, cals = _make_fake_client("meetings")
    cals[0].event_by_uid.return_value = fake_event

    settings = _make_settings()

    with (
        patch("pynchy.integrations.plugins.caldav.get_settings", return_value=settings),
        patch("pynchy.integrations.plugins.caldav._get_caldav_client", return_value=fake_client),
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
        patch("pynchy.integrations.plugins.caldav.get_settings", return_value=settings),
        patch(
            "pynchy.integrations.plugins.caldav._get_caldav_client",
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

    with patch("pynchy.integrations.plugins.caldav.get_settings", return_value=settings):
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

    settings = _make_settings()
    settings.data_dir = tmp_path

    deps = FakeDeps({"test@g.us": TEST_GROUP})

    # Mock the plugin manager to return our CalDAV handlers
    from pynchy.integrations.plugins.caldav import CalDAVMcpServerPlugin

    fake_pm = MagicMock()
    fake_pm.hook.pynchy_service_handler.return_value = [
        CalDAVMcpServerPlugin().pynchy_service_handler(),
    ]

    with (
        patch("pynchy.ipc._handlers_service.get_settings", return_value=settings),
        patch("pynchy.ipc._handlers_service.get_plugin_manager", return_value=fake_pm),
        patch("pynchy.integrations.plugins.caldav.get_settings", return_value=settings),
        patch("pynchy.integrations.plugins.caldav._get_caldav_client", return_value=fake_client),
    ):
        data = {
            "type": "service:list_calendar",
            "request_id": "cal-req-1",
            "start_date": "2026-02-16T00:00:00+00:00",
            "end_date": "2026-02-17T00:00:00+00:00",
            "calendar": "primary",
        }
        await _handle_service_request(data, "test-ws", False, deps)

    response_file = tmp_path / "ipc" / "test-ws" / "responses" / "cal-req-1.json"
    assert response_file.exists()
    response = json.loads(response_file.read_text())
    assert "result" in response
    assert response["result"]["count"] == 1
    assert response["result"]["events"][0]["uid"] == "e2e-1"
