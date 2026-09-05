"""Shared fixtures and helpers for live integration tests.

Live tests require real service connections and are skipped by default.
Run with: uv run pytest tests/live/ -m live
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from pynchy.event_bus import AgentTraceEvent, MessageEvent
from pynchy.host.orchestrator.app import PynchyApp
from pynchy.state import init_test_database
from pynchy.workspace.api import WorkspaceProfile

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Channel stubs — each mimics the real channel's protocol surface
# ---------------------------------------------------------------------------


@dataclass
class RecordingChannel:
    """Base recording channel that captures all sent messages.

    Mimics the Channel protocol with configurable behavior flags to match
    the characteristics of each real channel implementation.
    """

    name: str = "recording"
    connected: bool = True
    # Whether outbound messages are prefixed with assistant name.
    # WhatsApp: True; Slack and Discord: False (the platform shows bot identity).
    prefix_assistant_name: bool = True

    # Captured outputs
    sent_messages: list[tuple[str, str]] = field(default_factory=list)
    posted_messages: list[tuple[str, str]] = field(default_factory=list)
    updated_messages: list[tuple[str, str, str]] = field(default_factory=list)
    reactions: list[tuple[str, str, str, str]] = field(default_factory=list)
    typing_states: list[tuple[str, bool]] = field(default_factory=list)

    # Streaming support
    supports_streaming: bool = False  # noqa: V107
    _post_counter: int = 0

    async def connect(self) -> None:
        self.connected = True

    async def send_event(self, jid: str, event: object) -> None:
        content = event.content if hasattr(event, "content") else str(event)
        self.sent_messages.append((jid, content))

    def is_connected(self) -> bool:
        return self.connected

    def owns_jid(self, jid: str) -> bool:
        return True  # Accept all JIDs for testing

    async def disconnect(self) -> None:
        self.connected = False

    async def set_typing(self, jid: str, *, is_typing: bool) -> None:
        self.typing_states.append((jid, is_typing))

    async def send_reaction(self, jid: str, message_id: str, sender: str, emoji: str) -> None:
        self.reactions.append((jid, message_id, sender, emoji))

    def get_texts(self, jid: str | None = None) -> list[str]:
        """Get all sent message texts, optionally filtered by JID."""
        if jid:
            return [text for j, text in self.sent_messages if j == jid]
        return [text for _, text in self.sent_messages]

    def clear(self) -> None:
        """Reset all captured state."""
        self.sent_messages.clear()
        self.posted_messages.clear()
        self.updated_messages.clear()
        self.reactions.clear()
        self.typing_states.clear()


@dataclass
class StreamingChannel(RecordingChannel):
    """Channel that supports streaming (post_event + update_event).

    Mirrors Slack's streaming capability where messages are posted first
    and then updated in-place as content streams in.
    """

    supports_streaming: bool = True  # noqa: V107
    _post_counter: int = 0

    async def post_event(self, jid: str, event: object) -> str | None:
        self._post_counter += 1
        msg_id = f"msg-{self._post_counter}"
        content = event.content if hasattr(event, "content") else str(event)
        self.posted_messages.append((jid, content))
        return msg_id

    async def update_event(self, jid: str, message_id: str, event: object) -> None:
        content = event.content if hasattr(event, "content") else str(event)
        self.updated_messages.append((jid, message_id, content))


def make_whatsapp_channel() -> RecordingChannel:
    """WhatsApp channel stub.

    WhatsApp prefixes messages with assistant name and supports reactions.
    No streaming support (messages are sent as complete units).
    """
    return RecordingChannel(name="whatsapp", prefix_assistant_name=True)


def make_slack_channel() -> StreamingChannel:
    """Slack channel stub.

    Slack does NOT prefix assistant name (the bot identity is shown by the
    platform). Supports streaming via post_event + update_event.
    """
    return StreamingChannel(name="slack", prefix_assistant_name=False)


def make_discord_channel() -> StreamingChannel:
    """Discord channel stub.

    Like Slack, Discord shows the bot identity itself (no assistant-name
    prefix) and streams via post_event + update_event (in-place message edits).
    """
    return StreamingChannel(name="discord", prefix_assistant_name=False)


# ---------------------------------------------------------------------------
# EventBus capture
# ---------------------------------------------------------------------------


class EventCapture:
    """Captures EventBus emissions for parity assertions."""

    def __init__(self, event_bus: Any) -> None:
        self.traces: list[AgentTraceEvent] = []
        self.messages: list[MessageEvent] = []
        event_bus.subscribe(AgentTraceEvent, self._on_trace)
        event_bus.subscribe(MessageEvent, self._on_message)

    async def _on_trace(self, event: AgentTraceEvent) -> None:
        self.traces.append(event)

    async def _on_message(self, event: MessageEvent) -> None:
        self.messages.append(event)

    async def drain(self) -> None:
        """Let pending event callbacks run."""
        await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# Container process simulation
# ---------------------------------------------------------------------------


class FakeProcess:
    """Simulates asyncio.subprocess.Process for container output."""

    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self._returncode: int | None = None
        self._wait_event = asyncio.Event()
        self.pid = 12345

    def finish(self) -> None:
        self._returncode = 0
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._wait_event.set()

    async def wait(self) -> int:
        await self._wait_event.wait()
        return self._returncode  # type: ignore[return-value]

    def kill(self) -> None:
        pass

    @property
    def returncode(self) -> int | None:
        return self._returncode


class _FakeStdin:
    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data += data

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Settings and app helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def live_app(tmp_path: Path) -> PynchyApp:
    """Create a PynchyApp configured for live testing."""
    await init_test_database()
    app = PynchyApp()
    app.workspaces = {
        "group@g.us": WorkspaceProfile(
            jid="group@g.us",
            name="Test Group",
            folder="test-group",
            trigger="@pynchy",
            added_at="2024-01-01T00:00:00.000Z",
        ),
    }
    return app


@pytest.fixture
def all_channels() -> dict[str, RecordingChannel]:
    """Create one instance of each channel type for parity testing."""
    return {
        "whatsapp": make_whatsapp_channel(),
        "slack": make_slack_channel(),
    }
