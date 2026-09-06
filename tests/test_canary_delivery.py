"""Synthetic input reports provider admission without queuing ordinary replies."""

from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import NullChannel

from pynchy.host.orchestrator.app import PynchyApp
from pynchy.host.orchestrator.dep_factory import make_http_deps
from pynchy.host.orchestrator.http_control import ControlPlaneRuntime, RequestRateLimiter
from pynchy.host.orchestrator.http_server import create_http_app
from pynchy.identifiers import ChannelName, ChatJid
from pynchy.plugins.api import OutboundEvent, OutboundEventType
from pynchy.state.api import get_pending_outbound, init_test_database


class CanaryChannel(NullChannel):
    name = "discord"

    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        self.received: list[OutboundEvent] = []

    def owns_jid(self, jid: str) -> bool:
        return jid.startswith("discord:channel:")

    def is_connected(self) -> bool:
        return self.outcome != "disconnected"

    async def send_event(self, jid: str, event: OutboundEvent) -> None:
        if self.outcome == "rejected":
            raise OSError("404 Not Found: Unknown Channel")
        self.received.append(event)


@pytest.mark.parametrize("outcome", ["accepted", "rejected", "disconnected", "unconfigured"])
async def test_canary_admission_matches_provider_delivery(outcome: str) -> None:
    await init_test_database()
    app = PynchyApp()
    channel = CanaryChannel(outcome)
    app.channels = [] if outcome == "unconfigured" else [channel]
    runtime = ControlPlaneRuntime(
        bind_host="127.0.0.1",
        port=8484,
        unix_socket=None,
        public_bind=False,
        remote_auth_required=False,
        allow_remote_deploy=False,
        auth_token=None,
        rate_limiter=RequestRateLimiter(request_limit=20, window_seconds=60),
        audit_security_event=AsyncMock(),
    )
    jid = ChatJid("discord:channel:missing")
    async with TestClient(
        TestServer(create_http_app(make_http_deps(app), runtime=runtime))
    ) as client:
        response = await client.post("/canaries/messages", json={"jid": jid, "content": "hello"})
        assert response.status == (200 if outcome == "accepted" else 503)
        if outcome == "accepted":
            assert await response.json() == {"status": "accepted"}
            assert channel.received[0].metadata == {"synthetic_user_input": True}
        else:
            assert await response.json() == {"error": "Synthetic input was not delivered"}
    assert await get_pending_outbound(ChannelName("discord"), jid) == []


@pytest.mark.parametrize("outcome", ["rejected", "disconnected"])
async def test_ordinary_outbound_keeps_failed_delivery_for_retry(outcome: str) -> None:

    await init_test_database()
    app = PynchyApp()
    app.channels = [CanaryChannel(outcome)]
    jid = ChatJid("discord:channel:retry")
    await app.broadcast_to_channels(
        jid, OutboundEvent(type=OutboundEventType.TEXT, content="reply")
    )
    pending = await get_pending_outbound(ChannelName("discord"), jid)
    assert [delivery.content for delivery in pending] == ["reply"]
