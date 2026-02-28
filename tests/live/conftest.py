"""Shared fixtures and helpers for live integration tests.

Live tests require real service connections and are skipped by default.
Run with: uv run pytest tests/live/ -m live
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from conftest import make_settings

from pynchy.host.orchestrator.app import PynchyApp
from pynchy.state import _init_test_database
from pynchy.event_bus import AgentTraceEvent, MessageEvent
from pynchy.types import NewMessage, WorkspaceProfile

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
    # WhatsApp: True, Slack: False (bot name shown by platform), TUI: N/A (SSE)
    prefix_assistant_name: bool = True

    # Captured outputs
    sent_messages: list[tuple[str, str]] = field(default_factory=list)
    posted_messages: list[tuple[str, str]] = field(default_factory=list)
    updated_messages: list[tuple[str, str, str]] = field(default_factory=list)
    reactions: list[tuple[str, str, str, str]] = field(default_factory=list)
    typing_states: list[tuple[str, bool]] = field(default_factory=list)

    # Streaming support
    supports_streaming: bool = False
    _post_counter: int = 0

    async def connect(self) -> None:
        self.connected = True

    async def send_message(self, jid: str, text: str) -> None:
        self.sent_messages.append((jid, text))

    def is_connected(self) -> bool:
        return self.connected

    def owns_jid(self, jid: str) -> bool:
        return True  # Accept all JIDs for testing

    async def disconnect(self) -> None:
        self.connected = False

    async def set_typing(self, jid: str, is_typing: bool) -> None:
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
    """Channel that supports streaming (post_message + update_message).

    Mirrors Slack's streaming capability where messages are posted first
    and then updated in-place as content streams in.
    """

    supports_streaming: bool = True
    _post_counter: int = 0

    async def post_message(self, jid: str, text: str) -> str | None:
        self._post_counter += 1
        msg_id = f"msg-{self._post_counter}"
        self.posted_messages.append((jid, text))
        return msg_id

    async def update_message(self, jid: str, message_id: str, text: str) -> None:
        self.updated_messages.append((jid, message_id, text))


def make_tui_channel() -> RecordingChannel:
    """TUI channel stub.

    TUI doesn't go through send_message — it uses SSE/EventBus. But for
    broadcast_to_channels tests, we create a recording channel that mimics
    how a TUI-like channel would behave if it were a Channel protocol impl.
    """
    return RecordingChannel(name="tui", prefix_assistant_name=True)


def make_whatsapp_channel() -> RecordingChannel:
    """WhatsApp channel stub.

    WhatsApp prefixes messages with assistant name and supports reactions.
    No streaming support (messages are sent as complete units).
    """
    return RecordingChannel(name="whatsapp", prefix_assistant_name=True)


def make_slack_channel() -> StreamingChannel:
    """Slack channel stub.

    Slack does NOT prefix assistant name (the bot identity is shown by the
    platform). Supports streaming via post_message + update_message.
    """
    return StreamingChannel(name="slack", prefix_assistant_name=False)


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

    def feed_output(self, output: dict[str, Any]) -> None:
        """Feed a single container output event via stdout (legacy, unused).

        Output is now file-based IPC; this method exists only for backward
        compatibility in live test fixtures that haven't been updated yet.
        """
        self.stdout.feed_data(json.dumps(output).encode())

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


@contextlib.contextmanager
def patch_test_settings(tmp_path: Path):
    """Patch settings accessors to use tmp test directories."""
    s = make_settings(
        project_root=tmp_path,
        groups_dir=tmp_path / "groups",
        data_dir=tmp_path / "data",
    )
    with contextlib.ExitStack() as stack:
        for mod in (
            "pynchy.host.container_manager.credentials",
            "pynchy.host.container_manager.mounts",
            "pynchy.host.container_manager.session_prep",
            "pynchy.host.container_manager.orchestrator",
            "pynchy.host.container_manager.snapshots",
            "pynchy.host.orchestrator.messaging.pipeline",
            "pynchy.host.orchestrator.messaging.router",
        ):
            stack.enter_context(patch(f"{mod}.get_settings", return_value=s))
        yield s


def make_test_message(
    *,
    chat_jid: str = "group@g.us",
    content: str = "@pynchy hello",
    sender: str = "user@s.whatsapp.net",
    sender_name: str = "Alice",
    msg_id: str = "m1",
    timestamp: str = "2024-01-01T00:00:01.000Z",
    message_type: str = "user",
) -> NewMessage:
    """Create a test NewMessage with sensible defaults."""
    return NewMessage(
        id=msg_id,
        chat_jid=chat_jid,
        sender=sender,
        sender_name=sender_name,
        content=content,
        timestamp=timestamp,
        message_type=message_type,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def live_app(tmp_path: Path) -> PynchyApp:
    """Create a PynchyApp configured for live testing."""
    await _init_test_database()
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
        "tui": make_tui_channel(),
        "whatsapp": make_whatsapp_channel(),
        "slack": make_slack_channel(),
    }
