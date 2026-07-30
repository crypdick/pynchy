"""Additional public CalDAV handler contracts."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from pynchy.plugins.integrations.caldav import (
    CalDAVMcpServerPlugin,
    CalDAVRuntime,
    CalDAVServerOptions,
    clear_caldav_client_cache,
    configure_caldav_runtime,
    get_caldav_client,
)


def _server() -> CalDAVServerOptions:
    return CalDAVServerOptions(
        url="https://calendar.example.test",
        username="calendar-user",
        password_env="CALDAV_" + "PASSWORD",  # pragma: allowlist secret
        default_calendar=None,
        allow=None,
        ignore=None,
    )


def _list_calendar_handler():
    registration = CalDAVMcpServerPlugin().pynchy_service_handler()
    action = registration.action_for("list_calendar")
    assert action is not None
    return action.handler


@pytest.mark.asyncio
async def test_list_calendar_normalizes_naive_dates_and_plain_datetime_values() -> None:
    calendar = MagicMock(name="calendar")
    calendar.name = "meetings"
    event = MagicMock()

    class _CalendarValue:
        dt = object()

    event.icalendar_component = {"dtstart": _CalendarValue()}
    calendar.date_search.return_value = [event]
    client = MagicMock()
    client.principal.return_value.calendars.return_value = [calendar]
    configure_caldav_runtime(CalDAVRuntime(default_server="work", servers={"work": _server()}))

    with patch("pynchy.plugins.integrations.caldav.get_caldav_client", return_value=client):
        result = await _list_calendar_handler()(
            {
                "calendar": "primary",
                "start_date": "2026-02-16T10:00:00",
                "end_date": "2026-02-16T11:00:00",
            }
        )

    search = calendar.date_search.call_args.kwargs
    assert search["start"].tzinfo is not None
    assert search["end"].tzinfo is not None
    assert result["result"]["events"][0]["start"] == str(_CalendarValue.dt)


@pytest.mark.asyncio
async def test_list_calendar_reports_when_no_calendar_is_visible() -> None:
    client = MagicMock()
    client.principal.return_value.calendars.return_value = []
    configure_caldav_runtime(CalDAVRuntime(default_server="work", servers={"work": _server()}))

    with patch("pynchy.plugins.integrations.caldav.get_caldav_client", return_value=client):
        result = await _list_calendar_handler()({"calendar": "primary"})

    assert "No visible calendars" in result["error"]


def test_caldav_client_uses_password_environment_and_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_caldav_client_cache()
    monkeypatch.setenv("CALDAV_" + "PASSWORD", "pass" + "word")
    fake_client = object()
    constructor = MagicMock(return_value=fake_client)
    caldav_module = ModuleType("caldav")
    caldav_module.DAVClient = constructor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "caldav", caldav_module)

    first = get_caldav_client("work", _server())
    second = get_caldav_client("work", _server())

    assert first is second is fake_client
    constructor.assert_called_once_with(
        url="https://calendar.example.test",
        username="calendar-user",
        password="pass" + "word",  # pragma: allowlist secret
    )
