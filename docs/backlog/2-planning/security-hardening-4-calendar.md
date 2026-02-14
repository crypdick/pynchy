# Security Hardening: Step 4 - Calendar Integration

## Overview

Implement the host-side calendar service adapter that processes calendar-related IPC requests using CalDAV or Google Calendar API.

## Scope

This step adds the calendar service integration layer on the host, enabling agents to read and manage calendar events through the policy-gated IPC mechanism. Calendar credentials are stored and used only by the host process.

## Dependencies

- ✅ Step 1: Workspace Security Profiles (must be complete)
- ✅ Step 2: MCP Tools & Basic Policy (must be complete)
- Step 3: Email Integration (optional, but establishes service integration pattern)

## Background

Agents can already call `list_calendar`, `create_event`, and `delete_event` MCP tools (from Step 2), which write IPC requests. This step implements the host-side handler that executes these operations against real calendar services.

## Implementation Options

Two approaches for calendar integration:

### Option A: CalDAV (Universal)
- Works with any CalDAV-compatible service (Google, iCloud, Nextcloud, Fastmail)
- Standard protocol
- Library: `caldav` (maintained, actively developed)

### Option B: Google Calendar API (Google-only)
- Richer features and better performance
- Requires OAuth2 and Google Cloud project
- Library: `google-api-python-client`

**Recommendation:** Start with Option A (CalDAV) for broad compatibility. Can add Option B later.

## Implementation

### 1. Calendar Configuration Schema

**File:** `src/pynchy/config/services.py`

Extend the existing `ServicesConfig`:

```python
from typing import Literal


class CalDAVConfig(TypedDict):
    """CalDAV server configuration."""

    url: str  # CalDAV server URL
    username: str
    password: str  # TODO: Encrypt or use keyring
    calendar_name: str | None  # Default calendar (None = primary)


class CalendarServiceConfig(TypedDict):
    """Calendar service configuration."""

    provider: Literal["caldav", "google_calendar"]
    caldav: CalDAVConfig | None


class ServicesConfig(TypedDict):
    """All service configurations."""

    email: EmailServiceConfig | None
    calendar: CalendarServiceConfig | None  # New field
```

### 2. Calendar Adapter

**File:** `src/pynchy/services/calendar_adapter.py` (new file)

```python
"""Calendar service adapter using CalDAV."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import caldav
from caldav.elements import dav
from icalendar import Calendar, Event

from pynchy.config.services import CalendarServiceConfig

logger = logging.getLogger(__name__)


class CalendarAdapter:
    """Adapter for calendar operations via CalDAV."""

    def __init__(self, config: CalendarServiceConfig):
        self.config = config
        if config["provider"] != "caldav":
            raise ValueError(f"Unsupported calendar provider: {config['provider']}")

        if not config["caldav"]:
            raise ValueError("CalDAV config required for caldav provider")

        self.caldav_config = config["caldav"]
        self._client = None
        self._calendar = None

    def _connect(self):
        """Establish CalDAV connection and cache client."""
        if self._client is None:
            self._client = caldav.DAVClient(
                url=self.caldav_config["url"],
                username=self.caldav_config["username"],
                password=self.caldav_config["password"],
            )

            principal = self._client.principal()
            calendars = principal.calendars()

            # Find the specified calendar or use primary
            calendar_name = self.caldav_config.get("calendar_name")
            if calendar_name:
                self._calendar = next(
                    (cal for cal in calendars if cal.name == calendar_name),
                    None,
                )
                if not self._calendar:
                    raise Exception(f"Calendar '{calendar_name}' not found")
            else:
                # Use first available calendar
                self._calendar = calendars[0] if calendars else None

            if not self._calendar:
                raise Exception("No calendars found")

    def list_events(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """List calendar events within date range.

        Args:
            start_date: ISO format date string (YYYY-MM-DD)
            end_date: ISO format date string (YYYY-MM-DD)

        Returns:
            List of event dicts with keys: id, title, start, end, description, location
        """
        self._connect()

        # Default to current month if no dates provided
        if not start_date:
            start_date = datetime.now().replace(day=1).strftime("%Y-%m-%d")
        if not end_date:
            end_date = datetime.now().replace(day=28).strftime("%Y-%m-%d")

        # Parse dates
        start = datetime.fromisoformat(start_date)
        end = datetime.fromisoformat(end_date)

        # Fetch events
        events = self._calendar.date_search(start=start, end=end)

        results = []
        for event in events:
            try:
                ical = Calendar.from_ical(event.data)
                for component in ical.walk():
                    if component.name == "VEVENT":
                        results.append({
                            "id": event.id,
                            "title": str(component.get("summary", "")),
                            "start": component.get("dtstart").dt.isoformat() if component.get("dtstart") else None,
                            "end": component.get("dtend").dt.isoformat() if component.get("dtend") else None,
                            "description": str(component.get("description", "")),
                            "location": str(component.get("location", "")),
                        })
            except Exception as e:
                logger.warning(f"Failed to parse event: {e}")
                continue

        return results

    def create_event(
        self,
        title: str,
        start: str,
        end: str,
        description: str | None = None,
        location: str | None = None,
    ) -> dict[str, Any]:
        """Create a calendar event.

        Args:
            title: Event title/summary
            start: ISO format datetime string
            end: ISO format datetime string
            description: Event description (optional)
            location: Event location (optional)

        Returns:
            Dict with created event details
        """
        self._connect()

        # Create iCalendar event
        cal = Calendar()
        event = Event()

        event.add("summary", title)
        event.add("dtstart", datetime.fromisoformat(start))
        event.add("dtend", datetime.fromisoformat(end))

        if description:
            event.add("description", description)
        if location:
            event.add("location", location)

        cal.add_component(event)

        # Save to CalDAV server
        created = self._calendar.save_event(cal.to_ical())

        return {
            "status": "created",
            "event_id": created.id,
            "title": title,
            "start": start,
            "end": end,
        }

    def delete_event(self, event_id: str) -> dict[str, Any]:
        """Delete a calendar event.

        Args:
            event_id: Event ID to delete

        Returns:
            Dict with deletion status
        """
        self._connect()

        # Find and delete event
        event = self._calendar.event_by_uid(event_id)
        if not event:
            raise Exception(f"Event {event_id} not found")

        event.delete()

        return {
            "status": "deleted",
            "event_id": event_id,
        }
```

### 3. Register Calendar Handler in IPC Watcher

**File:** `src/pynchy/ipc/watcher.py`

Update the initialization and request processing:

```python
from pynchy.services.calendar_adapter import CalendarAdapter

class IPCWatcher:
    def __init__(self, group_config: dict, services_config: dict):
        # ... existing init ...

        # Initialize calendar adapter
        if services_config.get("calendar"):
            self.calendar_adapter = CalendarAdapter(services_config["calendar"])
        else:
            self.calendar_adapter = None

    async def _process_allowed_request(self, tool_name: str, request: dict, request_id: str):
        """Process an allowed request using appropriate service adapter."""
        try:
            # ... existing email handlers ...

            if tool_name == "list_calendar":
                if not self.calendar_adapter:
                    raise Exception("Calendar service not configured")

                result = self.calendar_adapter.list_events(
                    start_date=request.get("start_date"),
                    end_date=request.get("end_date"),
                )

            elif tool_name == "create_event":
                if not self.calendar_adapter:
                    raise Exception("Calendar service not configured")

                result = self.calendar_adapter.create_event(
                    title=request["title"],
                    start=request["start"],
                    end=request["end"],
                    description=request.get("description"),
                    location=request.get("location"),
                )

            elif tool_name == "delete_event":
                if not self.calendar_adapter:
                    raise Exception("Calendar service not configured")

                result = self.calendar_adapter.delete_event(
                    event_id=request["event_id"],
                )

            else:
                # Other tools (passwords) - not yet implemented
                result = f"Handler for {tool_name} not yet implemented"

            # Send success response
            response_file = Path(f"/workspace/ipc/input/{request_id}_response.json")
            with open(response_file, "w") as f:
                json.dump({"result": result}, f)

        except Exception as e:
            logger.error(f"Error processing {tool_name}: {e}")
            await self._send_error_response(request_id, str(e))
```

### 4. Configuration Example

**File:** `config/services.json`

Add calendar configuration:

```json
{
  "email": { /* ... existing email config ... */ },
  "calendar": {
    "provider": "caldav",
    "caldav": {
      "url": "https://caldav.icloud.com",
      "username": "user@icloud.com",
      "password": "app-specific-password",
      "calendar_name": "Personal"
    }
  }
}
```

### Common CalDAV URLs:

- **Google Calendar**: `https://apidata.googleusercontent.com/caldav/v2/{email}/events`
- **iCloud**: `https://caldav.icloud.com`
- **Fastmail**: `https://caldav.fastmail.com`
- **Nextcloud**: `https://your-nextcloud.com/remote.php/dav`

## Dependencies

Add to `pyproject.toml`:

```toml
[project]
dependencies = [
    # ... existing ...
    "caldav>=1.3.9",
    "icalendar>=5.0.11",
]
```

## Tests

**File:** `tests/test_calendar_adapter.py`

```python
"""Tests for calendar adapter."""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime

from pynchy.services.calendar_adapter import CalendarAdapter


@pytest.fixture
def calendar_config():
    """Sample calendar config for testing."""
    return {
        "provider": "caldav",
        "caldav": {
            "url": "https://caldav.example.com",
            "username": "test@example.com",
            "password": "password",
            "calendar_name": "Test Calendar",
        },
    }


def test_calendar_adapter_init(calendar_config):
    """Test calendar adapter initialization."""
    adapter = CalendarAdapter(calendar_config)
    assert adapter.config == calendar_config


def test_calendar_adapter_invalid_provider():
    """Test calendar adapter rejects invalid provider."""
    config = {"provider": "invalid", "caldav": None}
    with pytest.raises(ValueError, match="Unsupported calendar provider"):
        CalendarAdapter(config)


@patch("caldav.DAVClient")
def test_list_events(mock_client, calendar_config):
    """Test listing calendar events."""
    # Mock CalDAV connection
    mock_principal = MagicMock()
    mock_calendar = MagicMock()
    mock_calendar.name = "Test Calendar"

    mock_client.return_value.principal.return_value = mock_principal
    mock_principal.calendars.return_value = [mock_calendar]

    # Mock event data
    mock_event = MagicMock()
    mock_event.id = "event-123"
    mock_event.data = b"BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Test Event\nEND:VEVENT\nEND:VCALENDAR"
    mock_calendar.date_search.return_value = [mock_event]

    adapter = CalendarAdapter(calendar_config)
    events = adapter.list_events()

    assert len(events) > 0
    mock_calendar.date_search.assert_called_once()


@patch("caldav.DAVClient")
def test_create_event(mock_client, calendar_config):
    """Test creating calendar event."""
    # Mock CalDAV connection
    mock_principal = MagicMock()
    mock_calendar = MagicMock()
    mock_calendar.name = "Test Calendar"

    mock_client.return_value.principal.return_value = mock_principal
    mock_principal.calendars.return_value = [mock_calendar]

    # Mock save response
    mock_created = MagicMock()
    mock_created.id = "event-456"
    mock_calendar.save_event.return_value = mock_created

    adapter = CalendarAdapter(calendar_config)
    result = adapter.create_event(
        title="Test Event",
        start="2026-02-15T10:00:00",
        end="2026-02-15T11:00:00",
    )

    assert result["status"] == "created"
    assert result["event_id"] == "event-456"
    mock_calendar.save_event.assert_called_once()


@patch("caldav.DAVClient")
def test_delete_event(mock_client, calendar_config):
    """Test deleting calendar event."""
    # Mock CalDAV connection
    mock_principal = MagicMock()
    mock_calendar = MagicMock()
    mock_calendar.name = "Test Calendar"

    mock_client.return_value.principal.return_value = mock_principal
    mock_principal.calendars.return_value = [mock_calendar]

    # Mock event lookup
    mock_event = MagicMock()
    mock_calendar.event_by_uid.return_value = mock_event

    adapter = CalendarAdapter(calendar_config)
    result = adapter.delete_event("event-789")

    assert result["status"] == "deleted"
    assert result["event_id"] == "event-789"
    mock_event.delete.assert_called_once()
```

## Security Considerations

1. **Credential Storage**
   - Same as email: use keyring or environment variables
   - Never expose credentials to container

2. **Event Validation**
   - Validate date formats (ISO 8601)
   - Prevent creating events too far in future (e.g., > 5 years)
   - Limit event duration (e.g., < 1 month)

3. **Access Control**
   - Verify user owns the calendar before modifications
   - For shared calendars, check write permissions

4. **Rate Limiting**
   - Limit event creation per hour/day
   - Prevent calendar spam

## Success Criteria

- [ ] Calendar adapter implemented with CalDAV support
- [ ] IPC watcher integrates calendar adapter
- [ ] Configuration schema extended
- [ ] Tests pass (adapter creation, mocked CalDAV calls)
- [ ] Dependencies added to `pyproject.toml`
- [ ] Documentation updated with setup instructions

## Documentation

Update the following:

1. **Setup guide** - How to configure calendar service (CalDAV URLs for major providers)
2. **Calendar tools reference** - Examples of listing/creating/deleting events
3. **Service configuration** - Add calendar section to services.json docs

## Provider-Specific Setup

### Google Calendar

1. Use CalDAV URL: `https://apidata.googleusercontent.com/caldav/v2/{email}/events`
2. Generate app-specific password (same as email)
3. Enable CalDAV in Google Calendar settings

### iCloud Calendar

1. Use CalDAV URL: `https://caldav.icloud.com`
2. Generate app-specific password: https://appleid.apple.com/
3. Username is full iCloud email

### Fastmail

1. Use CalDAV URL: `https://caldav.fastmail.com`
2. Use regular password or app password
3. Calendar name is shown in Fastmail settings

## Next Steps

After this is complete:
- Step 5: Password manager integration (1Password CLI)
- Step 6: Human approval gate (for delete_event, etc.)

## Future Enhancements

- Support Google Calendar API as alternative provider
- Add recurring event support
- Implement event reminders/alarms
- Support multiple calendars per workspace
- Add calendar sharing and invitations
- Implement event search and filtering
