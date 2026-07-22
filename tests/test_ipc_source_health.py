"""Read-only source-health projection from host runtimes to agents."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from conftest import NullChannel, NullIpcDeps, make_settings

from pynchy.config.models import WhatsAppConnectionConfig
from pynchy.config.source_health import MessagingSourceHealthConfig
from pynchy.host.container_manager.ipc import dispatch
from pynchy.host.container_manager.ipc.protocol import request_requires_idempotency_ledger
from pynchy.plugins.integrations.matrix_routing_config import MatrixConnectionConfig
from pynchy.state import init_test_database, store_message_direct
from pynchy.types import WorkspaceProfile

if TYPE_CHECKING:
    from pathlib import Path

    from pynchy.config import Settings


class _WhatsAppChannel(NullChannel):
    name = "personal-phone"

    def is_connected(self) -> bool:
        return True

    def owns_jid(self, jid: str) -> bool:
        return jid.endswith("@g.us")


class _SourceHealthDeps(NullIpcDeps):
    def __init__(self) -> None:
        self._workspaces = {
            "family@g.us": WorkspaceProfile(
                jid="family@g.us",
                name="Family",
                folder="family",
                trigger="@pynchy",
                added_at="2026-01-01T00:00:00+00:00",
            )
        }

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return self._workspaces

    def channels(self) -> list[_WhatsAppChannel]:
        return [_WhatsAppChannel()]


class _ConnectionHealthDeps(NullIpcDeps):
    def connection_statuses(self) -> dict[str, bool]:
        return {"connection.matrix.gateway": True}


@pytest.fixture
async def source_health_setup(monkeypatch, tmp_path):
    await init_test_database()
    settings = make_settings(
        data_dir=tmp_path,
        connections={"personal-phone": WhatsAppConnectionConfig()},
    )
    monkeypatch.setattr("pynchy.config.settings._state.settings", settings)
    await store_message_direct(
        message_id="inbound-1",
        chat_jid="family@g.us",
        sender="person@s.whatsapp.net",
        sender_name="Person",
        content="sensitive message body",
        timestamp="2026-07-22T01:18:48+00:00",
        is_from_me=False,
    )
    return settings


@pytest.mark.action("message.source.health")
async def test_reports_configured_health_and_precise_unconfigured_limits(
    source_health_setup: Settings,
) -> None:
    await dispatch(
        {
            "type": "messaging_source_health",
            "request_id": "health-request",
            "sources": ["whatsapp", "signal", "google_messages"],
        },
        "chat-manager",
        False,
        _SourceHealthDeps(),
    )

    response_path = (
        source_health_setup.data_dir / "ipc" / "chat-manager" / "responses" / "health-request.json"
    )
    response_text = response_path.read_text(encoding="utf-8")
    result = json.loads(response_text)["result"]

    assert result["sources"][0] == {
        "name": "personal-phone",
        "provider": "whatsapp",
        "status": "ready",
        "ready": True,
        "latest_inbound_at": "2026-07-22T01:18:48+00:00",
        "freshness_scope": "Pynchy-ingested inbound messages for registered workspaces",
        "reason": None,
    }
    assert [source["provider"] for source in result["sources"][1:]] == [
        "signal",
        "google_messages",
    ]
    assert all(source["status"] == "unavailable" for source in result["sources"][1:])
    assert all(
        source["reason"] == "Pynchy's host-local aggregate projection is not configured."
        for source in result["sources"][1:]
    )
    assert result["latest_inbound"] == {
        "name": "personal-phone",
        "provider": "whatsapp",
        "timestamp": "2026-07-22T01:18:48+00:00",
    }
    assert result["coverage"] == {
        "scope": "configured Pynchy runtimes and configured host-local aggregate stores",
        "message_content_read": False,
        "sender_identity_read": False,
        "provider_read_state_changed": False,
        "unknown_sources": "reported as not_established",
    }
    assert "sensitive message body" not in response_text


def _create_source_database(
    path: Path,
    *,
    latest_inbound_ms: int | None,
    collector_checked_ms: int | None = None,
) -> None:
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE messages ("
            "timestamp INTEGER NOT NULL, is_from_me INTEGER NOT NULL, "
            "sender TEXT NOT NULL, body TEXT NOT NULL)"
        )
        if latest_inbound_ms is not None:
            connection.execute(
                "INSERT INTO messages VALUES (?, 0, ?, ?)",
                (latest_inbound_ms, "private-contact", "private message body"),
            )
        if collector_checked_ms is not None:
            connection.execute("CREATE TABLE bridge_state (key TEXT, value TEXT)")
            connection.execute(
                "INSERT INTO bridge_state VALUES ('last_receive_ok_ms', ?)",
                (str(collector_checked_ms),),
            )


async def test_projects_host_aggregate_health_without_identity_or_body(
    monkeypatch, tmp_path
) -> None:
    await init_test_database()
    current = datetime.now(UTC).replace(microsecond=0)
    fresh_ms = int(current.timestamp() * 1000)
    stale_ms = int(datetime(2026, 6, 1, tzinfo=UTC).timestamp() * 1000)
    aggregate_root = tmp_path / "aggregate"
    _create_source_database(
        aggregate_root / "whatsapp" / "messages.db",
        latest_inbound_ms=fresh_ms,
    )
    _create_source_database(
        aggregate_root / "signal" / "messages.db",
        latest_inbound_ms=None,
        collector_checked_ms=stale_ms,
    )
    settings = make_settings(
        data_dir=tmp_path / "pynchy-data",
        messaging_source_health=MessagingSourceHealthConfig(data_dir=aggregate_root),
    )
    monkeypatch.setattr("pynchy.config.settings._state.settings", settings)

    await dispatch(
        {
            "type": "messaging_source_health",
            "request_id": "aggregate-health-request",
            "sources": ["whatsapp", "signal", "google_messages"],
        },
        "chat-manager",
        False,
        NullIpcDeps(),
    )

    response_path = (
        settings.data_dir / "ipc" / "chat-manager" / "responses" / "aggregate-health-request.json"
    )
    response_text = response_path.read_text(encoding="utf-8")
    result = json.loads(response_text)["result"]
    whatsapp, signal, google_messages = result["sources"]

    assert whatsapp == {
        "name": "whatsapp",
        "provider": "whatsapp",
        "status": "ready",
        "ready": True,
        "metadata_availability": "available",
        "collector_health": "not_observable",
        "event_freshness": "fresh",
        "latest_inbound_at": current.isoformat(),
        "freshness_scope": "Host-local aggregate records only; provider history is not complete",
        "reason": (
            "The aggregate store has a fresh inbound event; current collector process "
            "health is not independently observable."
        ),
    }
    assert signal["status"] == "unavailable"
    assert signal["metadata_availability"] == "available"
    assert signal["collector_health"] == "stale"
    assert signal["event_freshness"] == "unknown"
    assert google_messages["status"] == "unavailable"
    assert "complete Google Messages source" in google_messages["reason"]
    assert result["latest_inbound"] == {
        "name": "whatsapp",
        "provider": "whatsapp",
        "timestamp": current.isoformat(),
    }
    assert "private-contact" not in response_text
    assert "private message body" not in response_text


def test_source_health_requests_skip_mutation_ledger() -> None:
    assert not request_requires_idempotency_ledger("messaging_source_health")


async def test_reports_non_channel_connection_runtime_status(monkeypatch, tmp_path) -> None:
    await init_test_database()
    settings = make_settings(
        data_dir=tmp_path,
        connections={"gateway": MatrixConnectionConfig(expected_user_id="@owner:example.test")},
    )
    monkeypatch.setattr("pynchy.config.settings._state.settings", settings)

    await dispatch(
        {
            "type": "messaging_source_health",
            "request_id": "connection-health-request",
            "sources": ["matrix"],
        },
        "chat-manager",
        False,
        _ConnectionHealthDeps(),
    )

    response_path = (
        settings.data_dir / "ipc" / "chat-manager" / "responses" / "connection-health-request.json"
    )
    result = json.loads(response_path.read_text(encoding="utf-8"))["result"]
    assert result["sources"] == [
        {
            "name": "gateway",
            "provider": "matrix",
            "status": "ready",
            "ready": True,
            "latest_inbound_at": None,
            "freshness_scope": (
                "Provider freshness is not projected by this Pynchy connection runtime"
            ),
            "reason": None,
        }
    ]


async def test_host_ipc_omission_preserves_complete_source_inventory(monkeypatch, tmp_path) -> None:
    await init_test_database()
    settings = make_settings(
        data_dir=tmp_path,
        connections={"gateway": MatrixConnectionConfig(expected_user_id="@owner:example.test")},
    )
    monkeypatch.setattr("pynchy.config.settings._state.settings", settings)

    await dispatch(
        {
            "type": "messaging_source_health",
            "request_id": "complete-health-request",
        },
        "chat-manager",
        False,
        _ConnectionHealthDeps(),
    )

    response_path = (
        settings.data_dir / "ipc" / "chat-manager" / "responses" / "complete-health-request.json"
    )
    result = json.loads(response_path.read_text(encoding="utf-8"))["result"]
    assert [source["name"] for source in result["sources"]] == [
        "gateway",
        "whatsapp",
        "signal",
        "google_messages",
    ]


async def test_rejects_non_list_source_filter(source_health_setup: Settings) -> None:
    await dispatch(
        {
            "type": "messaging_source_health",
            "request_id": "invalid-health-request",
            "sources": "whatsapp",
        },
        "chat-manager",
        False,
        _SourceHealthDeps(),
    )

    response_path = (
        source_health_setup.data_dir
        / "ipc"
        / "chat-manager"
        / "responses"
        / "invalid-health-request.json"
    )
    assert json.loads(response_path.read_text(encoding="utf-8")) == {
        "error": "sources must be a list of source names"
    }


async def test_unknown_source_remains_not_established(monkeypatch, tmp_path) -> None:
    await init_test_database()
    settings = make_settings(data_dir=tmp_path)
    monkeypatch.setattr("pynchy.config.settings._state.settings", settings)

    await dispatch(
        {
            "type": "messaging_source_health",
            "request_id": "unknown-health-request",
            "sources": ["carrier_pigeon"],
        },
        "chat-manager",
        False,
        NullIpcDeps(),
    )

    response_path = (
        settings.data_dir / "ipc" / "chat-manager" / "responses" / "unknown-health-request.json"
    )
    result = json.loads(response_path.read_text(encoding="utf-8"))["result"]
    assert result["sources"][0]["status"] == "not_established"
    assert result["sources"][0]["provider"] is None
    assert result["latest_inbound"] is None
