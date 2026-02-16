"""Tests for CalDAV calendar integration via the MCP server plugin."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from pynchy.config import (
    CalDAVConfig,
    WorkspaceConfig,
    WorkspaceSecurityConfig,
)
from pynchy.db import _init_test_database
from pynchy.ipc._handlers_service import (
    _handle_service_request,
    clear_plugin_handler_cache,
    clear_policy_cache,
)
from pynchy.plugin.builtin_mcp_caldav import (
    _handle_create_event,
    _handle_delete_event,
    _handle_list_calendar,
    clear_caldav_client_cache,
)
from pynchy.types import RegisteredGroup


@pytest.fixture(autouse=True)
async def _setup():
    await _init_test_database()
    clear_policy_cache()
    clear_caldav_client_cache()
    clear_plugin_handler_cache()


class FakeDeps:
    def __init__(self, groups=None):
        self._groups = groups or {}

    def registered_groups(self):
        return self._groups


TEST_GROUP = RegisteredGroup(
    name="Test",
    folder="test-ws",
    trigger="@Pynchy",
    added_at="2024-01-01",
)

CALDAV_CONFIG = CalDAVConfig(
    url="https://nextcloud.example.com/remote.php/dav/",
    username="testuser",
    password="testpass",
    default_calendar="personal",
)

EMPTY_CALDAV_CONFIG = CalDAVConfig()


def _make_settings(caldav_cfg=CALDAV_CONFIG, ws_security=None):
    """Create fake settings with CalDAV and workspace security configured."""

    class FakeSettings:
        def __init__(self):
            self.caldav = caldav_cfg
            self.workspaces = {
                "test-ws": WorkspaceConfig(
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


# ---------------------------------------------------------------------------
# Not-configured tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_calendar_not_configured():
    """Returns error when CalDAV URL is empty."""
    settings = _make_settings(caldav_cfg=EMPTY_CALDAV_CONFIG)
    with patch("pynchy.plugin.builtin_mcp_caldav.get_settings", return_value=settings):
        result = await _handle_list_calendar({"calendar": "primary"})
    assert "error" in result
    assert "not configured" in result["error"].lower()


@pytest.mark.asyncio
async def test_create_event_not_configured():
    settings = _make_settings(caldav_cfg=EMPTY_CALDAV_CONFIG)
    with patch("pynchy.plugin.builtin_mcp_caldav.get_settings", return_value=settings):
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
    with patch("pynchy.plugin.builtin_mcp_caldav.get_settings", return_value=settings):
        result = await _handle_delete_event({"event_id": "uid-123", "calendar": "primary"})
    assert "error" in result
    assert "not configured" in result["error"].lower()


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

    fake_cal = MagicMock()
    fake_cal.date_search.return_value = [fake_event]
    fake_cal.name = "personal"

    fake_principal = MagicMock()
    fake_principal.calendars.return_value = [fake_cal]

    fake_client = MagicMock()
    fake_client.principal.return_value = fake_principal

    settings = _make_settings()

    with (
        patch("pynchy.plugin.builtin_mcp_caldav.get_settings", return_value=settings),
        patch("pynchy.plugin.builtin_mcp_caldav._get_caldav_client", return_value=fake_client),
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
    fake_cal = MagicMock()
    fake_cal.date_search.return_value = []
    fake_cal.name = "personal"

    fake_principal = MagicMock()
    fake_principal.calendars.return_value = [fake_cal]

    fake_client = MagicMock()
    fake_client.principal.return_value = fake_principal

    settings = _make_settings()

    with (
        patch("pynchy.plugin.builtin_mcp_caldav.get_settings", return_value=settings),
        patch("pynchy.plugin.builtin_mcp_caldav._get_caldav_client", return_value=fake_client),
    ):
        result = await _handle_list_calendar({"calendar": "primary"})

    assert "result" in result
    assert result["result"]["count"] == 0

    # Verify date_search was called with a ~7 day range
    call_args = fake_cal.date_search.call_args
    start = call_args.kwargs["start"]
    end = call_args.kwargs["end"]
    diff = end - start
    assert 6 <= diff.days <= 7


@pytest.mark.asyncio
async def test_list_calendar_primary_maps_to_default():
    """'primary' calendar name maps to CalDAVConfig.default_calendar."""
    fake_cal = MagicMock()
    fake_cal.date_search.return_value = []
    fake_cal.name = "my-cal"

    fake_principal = MagicMock()
    fake_principal.calendars.return_value = [fake_cal]

    fake_client = MagicMock()
    fake_client.principal.return_value = fake_principal

    cfg = CalDAVConfig(
        url="https://example.com/dav/",
        username="user",
        password="pass",
        default_calendar="my-cal",
    )
    settings = _make_settings(caldav_cfg=cfg)

    with (
        patch("pynchy.plugin.builtin_mcp_caldav.get_settings", return_value=settings),
        patch("pynchy.plugin.builtin_mcp_caldav._get_caldav_client", return_value=fake_client),
    ):
        result = await _handle_list_calendar({"calendar": "primary"})

    assert "result" in result


@pytest.mark.asyncio
async def test_list_calendar_not_found():
    """Error when requested calendar doesn't exist."""
    fake_cal = MagicMock()
    fake_cal.name = "other-cal"

    fake_principal = MagicMock()
    fake_principal.calendars.return_value = [fake_cal]

    fake_client = MagicMock()
    fake_client.principal.return_value = fake_principal

    settings = _make_settings()

    with (
        patch("pynchy.plugin.builtin_mcp_caldav.get_settings", return_value=settings),
        patch("pynchy.plugin.builtin_mcp_caldav._get_caldav_client", return_value=fake_client),
    ):
        result = await _handle_list_calendar({"calendar": "nonexistent"})

    assert "error" in result
    assert "not found" in result["error"].lower()


# ---------------------------------------------------------------------------
# create_event tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_event_success():
    """create_event calls save_event and returns UID."""
    created_event = _make_fake_event(uid="new-uid-1")

    fake_cal = MagicMock()
    fake_cal.save_event.return_value = created_event
    fake_cal.name = "personal"

    fake_principal = MagicMock()
    fake_principal.calendars.return_value = [fake_cal]

    fake_client = MagicMock()
    fake_client.principal.return_value = fake_principal

    settings = _make_settings()

    with (
        patch("pynchy.plugin.builtin_mcp_caldav.get_settings", return_value=settings),
        patch("pynchy.plugin.builtin_mcp_caldav._get_caldav_client", return_value=fake_client),
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
    call_kwargs = fake_cal.save_event.call_args.kwargs
    assert call_kwargs["summary"] == "New Meeting"
    assert call_kwargs["description"] == "Discuss plans"
    assert call_kwargs["location"] == "Office"
    assert isinstance(call_kwargs["dtstart"], datetime)
    assert isinstance(call_kwargs["dtend"], datetime)


@pytest.mark.asyncio
async def test_create_event_minimal():
    """create_event works with only required fields (no description/location)."""
    created_event = _make_fake_event(uid="min-uid")

    fake_cal = MagicMock()
    fake_cal.save_event.return_value = created_event
    fake_cal.name = "personal"

    fake_principal = MagicMock()
    fake_principal.calendars.return_value = [fake_cal]

    fake_client = MagicMock()
    fake_client.principal.return_value = fake_principal

    settings = _make_settings()

    with (
        patch("pynchy.plugin.builtin_mcp_caldav.get_settings", return_value=settings),
        patch("pynchy.plugin.builtin_mcp_caldav._get_caldav_client", return_value=fake_client),
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
    call_kwargs = fake_cal.save_event.call_args.kwargs
    assert "description" not in call_kwargs
    assert "location" not in call_kwargs


# ---------------------------------------------------------------------------
# delete_event tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_event_success():
    """delete_event calls event.delete() and returns confirmation."""
    fake_event = MagicMock()

    fake_cal = MagicMock()
    fake_cal.event_by_uid.return_value = fake_event
    fake_cal.name = "personal"

    fake_principal = MagicMock()
    fake_principal.calendars.return_value = [fake_cal]

    fake_client = MagicMock()
    fake_client.principal.return_value = fake_principal

    settings = _make_settings()

    with (
        patch("pynchy.plugin.builtin_mcp_caldav.get_settings", return_value=settings),
        patch("pynchy.plugin.builtin_mcp_caldav._get_caldav_client", return_value=fake_client),
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
    fake_cal.event_by_uid.assert_called_once_with("uid-to-delete")
    fake_event.delete.assert_called_once()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_caldav_connection_error():
    """CalDAV connection failure returns error response."""
    settings = _make_settings()

    with (
        patch("pynchy.plugin.builtin_mcp_caldav.get_settings", return_value=settings),
        patch(
            "pynchy.plugin.builtin_mcp_caldav._get_caldav_client",
            side_effect=Exception("Connection refused"),
        ),
    ):
        result = await _handle_list_calendar({"calendar": "primary"})

    assert "error" in result
    assert "Connection refused" in result["error"]


# ---------------------------------------------------------------------------
# End-to-end: service request dispatches to CalDAV plugin handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calendar_tool_dispatches_to_plugin_handler(tmp_path):
    """Calendar service requests go through policy and dispatch to CalDAV plugin handler."""
    fake_event = _make_fake_event(uid="e2e-1", summary="E2E Test")
    fake_cal = MagicMock()
    fake_cal.date_search.return_value = [fake_event]
    fake_cal.name = "personal"

    fake_principal = MagicMock()
    fake_principal.calendars.return_value = [fake_cal]

    fake_client = MagicMock()
    fake_client.principal.return_value = fake_principal

    settings = _make_settings()
    settings.data_dir = tmp_path

    deps = FakeDeps({"test@g.us": TEST_GROUP})

    # Mock the plugin manager to return our CalDAV handlers
    from pynchy.plugin.builtin_mcp_caldav import CalDAVMcpServerPlugin

    fake_pm = MagicMock()
    fake_pm.hook.pynchy_mcp_server_handler.return_value = [
        CalDAVMcpServerPlugin().pynchy_mcp_server_handler(),
    ]

    with (
        patch("pynchy.ipc._handlers_service.get_settings", return_value=settings),
        patch("pynchy.ipc._handlers_service.get_plugin_manager", return_value=fake_pm),
        patch("pynchy.plugin.builtin_mcp_caldav.get_settings", return_value=settings),
        patch("pynchy.plugin.builtin_mcp_caldav._get_caldav_client", return_value=fake_client),
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
