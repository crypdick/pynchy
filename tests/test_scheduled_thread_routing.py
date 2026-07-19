"""Tests for channel capability routing used by scheduled task threads."""

from __future__ import annotations

from typing import Any

import pytest

from pynchy.host.orchestrator.app import PynchyApp
from pynchy.types import InboundFetchResult, OutboundEvent


class _ThreadCapableChannel:
    name = "test"
    formatter: Any = object()

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, tuple[str, ...]]] = []
        self.reused_participants: list[tuple[str, tuple[str, ...]]] = []

    async def connect(self) -> None: ...

    async def send_event(self, jid: str, event: OutboundEvent) -> None: ...

    def is_connected(self) -> bool:
        return True

    def owns_jid(self, jid: str) -> bool:
        return jid in {"discord:channel:parent", "discord:channel:existing"}

    async def disconnect(self) -> None: ...

    async def reconnect(self) -> None: ...

    def prepare_shutdown(self) -> None: ...

    async def fetch_inbound_since(self, channel_jid: str, since: str) -> InboundFetchResult:
        return InboundFetchResult(messages=[])

    async def create_thread(
        self,
        parent_jid: str,
        name: str,
        *,
        participant_ids: tuple[str, ...] = (),
    ) -> str:
        self.requests.append((parent_jid, name, participant_ids))
        return "discord:channel:child"

    async def find_thread(self, parent_jid: str, name: str) -> str | None:
        return "discord:channel:existing" if name == "pynchy-dev-1" else None

    async def add_thread_participants(
        self,
        child_jid: str,
        participant_ids: tuple[str, ...],
    ) -> None:
        self.reused_participants.append((child_jid, participant_ids))


@pytest.mark.asyncio
async def test_app_routes_thread_creation_to_owning_channel() -> None:
    app = PynchyApp()
    channel = _ThreadCapableChannel()
    app.channels = [channel]

    child_jid = await app.create_thread(
        "discord:channel:parent",
        "pynchy-dev-1",
        participant_ids=("123",),
    )

    assert child_jid == "discord:channel:child"
    assert channel.requests == [("discord:channel:parent", "pynchy-dev-1", ("123",))]


@pytest.mark.asyncio
async def test_app_rejects_thread_creation_without_channel_capability() -> None:
    app = PynchyApp()

    with pytest.raises(RuntimeError, match="does not support thread creation"):
        await app.create_thread("slack:C123", "pynchy-dev-1")


@pytest.mark.asyncio
async def test_app_finds_existing_thread_on_owning_channel() -> None:
    app = PynchyApp()
    channel = _ThreadCapableChannel()
    app.channels = [channel]

    child_jid = await app.find_thread("discord:channel:parent", "pynchy-dev-1")

    assert child_jid == "discord:channel:existing"


@pytest.mark.asyncio
async def test_app_adds_participants_to_existing_thread() -> None:
    app = PynchyApp()
    channel = _ThreadCapableChannel()
    app.channels = [channel]

    await app.add_thread_participants("discord:channel:existing", ("123",))

    assert channel.reused_participants == [("discord:channel:existing", ("123",))]
