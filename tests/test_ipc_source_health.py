"""Read-only source-health projection from host runtimes to agents."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from conftest import NullChannel, NullIpcDeps, make_settings

from pynchy.config.models import WhatsAppConnectionConfig
from pynchy.host.container_manager.ipc import dispatch
from pynchy.host.container_manager.ipc.protocol import request_requires_idempotency_ledger
from pynchy.state import init_test_database, store_message_direct
from pynchy.types import WorkspaceProfile

if TYPE_CHECKING:
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

    assert result["sources"] == [
        {
            "name": "personal-phone",
            "provider": "whatsapp",
            "status": "ready",
            "ready": True,
            "latest_inbound_at": "2026-07-22T01:18:48+00:00",
            "freshness_scope": "Pynchy-ingested inbound messages for registered workspaces",
            "reason": None,
        },
        {
            "name": "signal",
            "provider": None,
            "status": "not_established",
            "ready": False,
            "latest_inbound_at": None,
            "reason": (
                "No configured Pynchy connection matches this source name or provider type. "
                "Pynchy cannot inspect its health or freshness."
            ),
        },
        {
            "name": "google_messages",
            "provider": None,
            "status": "not_established",
            "ready": False,
            "latest_inbound_at": None,
            "reason": (
                "No configured Pynchy connection matches this source name or provider type. "
                "Pynchy cannot inspect its health or freshness."
            ),
        },
    ]
    assert result["coverage"] == {
        "scope": "configured Pynchy channel and connection runtimes only",
        "message_content_read": False,
        "provider_read_state_changed": False,
        "unconfigured_sources": "reported as not_established",
    }
    assert "sensitive message body" not in response_text


def test_source_health_requests_skip_mutation_ledger() -> None:
    assert not request_requires_idempotency_ledger("messaging_source_health")


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
