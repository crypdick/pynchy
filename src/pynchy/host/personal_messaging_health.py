"""Privacy-preserving health snapshots for host-local personal messaging sources.

The projection deliberately selects aggregate timestamps and collector state
only. Message bodies and sender identities never cross this boundary.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

PersonalProvider = Literal["whatsapp", "signal", "google_messages"]

PERSONAL_PROVIDERS: tuple[PersonalProvider, ...] = (
    "whatsapp",
    "signal",
    "google_messages",
)

_ALIASES: dict[str, PersonalProvider] = {
    "whatsapp": "whatsapp",
    "signal": "signal",
    "googlemessages": "google_messages",
    "googlemessage": "google_messages",
}


def personal_provider_for(source_name: str) -> PersonalProvider | None:
    """Resolve a user-facing source label to a supported personal provider."""
    normalized = "".join(character for character in source_name.casefold() if character.isalnum())
    return _ALIASES.get(normalized)


@dataclass(frozen=True)
class _AggregateState:
    readable: bool
    latest_inbound_at: datetime | None
    collector_checked_at: datetime | None = None


def _database_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro"


def _milliseconds_datetime(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (str, bytes, int, float)):
        return None
    try:
        milliseconds = int(value)
        return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _latest_inbound(connection: sqlite3.Connection) -> datetime | None:
    row = connection.execute("SELECT MAX(timestamp) FROM messages WHERE is_from_me = 0").fetchone()
    return _milliseconds_datetime(row[0] if row else None)


def _read_whatsapp(path: Path) -> _AggregateState:
    try:
        with closing(sqlite3.connect(_database_uri(path), uri=True)) as connection:
            return _AggregateState(readable=True, latest_inbound_at=_latest_inbound(connection))
    except sqlite3.Error:
        return _AggregateState(readable=False, latest_inbound_at=None)


def _signal_collector_checked(connection: sqlite3.Connection) -> datetime | None:
    try:
        row = connection.execute(
            "SELECT value FROM bridge_state WHERE key = 'last_receive_ok_ms'"
        ).fetchone()
    except sqlite3.Error:
        return None
    return _milliseconds_datetime(row[0] if row else None)


def _read_signal(path: Path) -> _AggregateState:
    try:
        with closing(sqlite3.connect(_database_uri(path), uri=True)) as connection:
            latest = _latest_inbound(connection)
            return _AggregateState(
                readable=True,
                latest_inbound_at=latest,
                collector_checked_at=_signal_collector_checked(connection),
            )
    except sqlite3.Error:
        return _AggregateState(readable=False, latest_inbound_at=None)


def _freshness(timestamp: datetime | None, *, cutoff: datetime) -> str:
    if timestamp is None:
        return "unknown"
    return "fresh" if timestamp >= cutoff else "stale"


def _unavailable(provider: PersonalProvider, reason: str) -> dict[str, object]:
    return {
        "name": provider,
        "provider": provider,
        "status": "unavailable",
        "ready": False,
        "metadata_availability": "unavailable",
        "collector_health": "unknown",
        "event_freshness": "unknown",
        "latest_inbound_at": None,
        "freshness_scope": "Host-local aggregate records only; provider history is not complete",
        "reason": reason,
    }


def _project_state(
    provider: PersonalProvider,
    state: _AggregateState,
    *,
    now: datetime,
    stale_after_hours: int,
) -> dict[str, object]:
    if not state.readable:
        return _unavailable(
            provider,
            "Pynchy could not read the configured host-local aggregate store.",
        )

    cutoff = now - timedelta(hours=stale_after_hours)
    event_freshness = _freshness(state.latest_inbound_at, cutoff=cutoff)
    if provider == "signal":
        collector_freshness = _freshness(state.collector_checked_at, cutoff=cutoff)
        collector_health = "healthy" if collector_freshness == "fresh" else collector_freshness
        ready = collector_health == "healthy"
    else:
        collector_health = "not_observable"
        ready = event_freshness == "fresh"

    reason: str | None = None
    if provider == "whatsapp" and ready:
        reason = (
            "The aggregate store has a fresh inbound event; current collector process "
            "health is not independently observable."
        )
    elif provider == "whatsapp":
        reason = "The aggregate store has no fresh inbound event."
    elif not ready:
        reason = "The collector's last successful receive check is stale or unavailable."

    return {
        "name": provider,
        "provider": provider,
        "status": "ready" if ready else "unavailable",
        "ready": ready,
        "metadata_availability": "available",
        "collector_health": collector_health,
        "event_freshness": event_freshness,
        "latest_inbound_at": (
            state.latest_inbound_at.isoformat() if state.latest_inbound_at else None
        ),
        "freshness_scope": "Host-local aggregate records only; provider history is not complete",
        "reason": reason,
    }


async def project_personal_source(
    provider: PersonalProvider,
    *,
    data_dir: Path | None,
    stale_after_hours: int,
    now: datetime | None = None,
) -> dict[str, object]:
    """Return a Pynchy-owned, body-free health projection for one source."""
    if data_dir is None:
        return _unavailable(
            provider,
            "Pynchy's host-local aggregate projection is not configured.",
        )
    if provider == "google_messages":
        return _unavailable(
            provider,
            "No body-free durable projection covers the complete Google Messages source.",
        )

    database = data_dir / provider / "messages.db"
    reader = _read_signal if provider == "signal" else _read_whatsapp
    state = await asyncio.to_thread(reader, database)
    return _project_state(
        provider,
        state,
        now=now or datetime.now(UTC),
        stale_after_hours=stale_after_hours,
    )
