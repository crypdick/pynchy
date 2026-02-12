"""Integration tests for PynchyApp.

End-to-end tests that wire up real subsystems (DB, queue, message processing)
with mocked boundaries (WhatsApp channel, container subprocess, Apple Container CLI).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from pynchy.app import PynchyApp
from pynchy.config import OUTPUT_END_MARKER, OUTPUT_START_MARKER
from pynchy.db import _init_test_database, store_message
from pynchy.types import NewMessage, RegisteredGroup

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_message(
    *,
    chat_jid: str = "group@g.us",
    content: str = "@pynchy hello",
    timestamp: str = "2024-01-01T00:00:01.000Z",
    sender: str = "user@s.whatsapp.net",
    sender_name: str = "Alice",
    msg_id: str = "m1",
) -> NewMessage:
    return NewMessage(
        id=msg_id,
        chat_jid=chat_jid,
        sender=sender,
        sender_name=sender_name,
        content=content,
        timestamp=timestamp,
    )


def _marker_wrap(output: dict[str, Any]) -> bytes:
    payload = f"{OUTPUT_START_MARKER}\n{json.dumps(output)}\n{OUTPUT_END_MARKER}\n"
    return payload.encode()


class FakeChannel:
    """Minimal Channel implementation for testing."""

    def __init__(self) -> None:
        self.name = "test"
        self.connected = True
        self.sent_messages: list[tuple[str, str]] = []

    async def connect(self) -> None:
        self.connected = True

    async def send_message(self, jid: str, text: str) -> None:
        self.sent_messages.append((jid, text))

    def is_connected(self) -> bool:
        return self.connected

    def owns_jid(self, jid: str) -> bool:
        return jid.endswith("@g.us") or jid.endswith("@s.whatsapp.net")

    async def disconnect(self) -> None:
        self.connected = False


class FakeProcess:
    """Simulates asyncio.subprocess.Process for integration tests."""

    def __init__(self, output: dict[str, Any] | None = None) -> None:
        self.stdin = FakeStdin()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self._returncode: int | None = None
        self._wait_event = asyncio.Event()
        self.pid = 12345
        self._output = output

    async def schedule_output(self) -> None:
        """Emit output and close after a short delay."""
        await asyncio.sleep(0.01)
        if self._output:
            self.stdout.feed_data(_marker_wrap(self._output))
        await asyncio.sleep(0.01)
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


class FakeStdin:
    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data += data

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def app(tmp_path: Path):
    """Create a PynchyApp with a fresh in-memory DB and patched dirs."""
    await _init_test_database()
    a = PynchyApp()
    a.registered_groups = {
        "group@g.us": RegisteredGroup(
            name="Test Group",
            folder="test-group",
            trigger="@pynchy",
            added_at="2024-01-01T00:00:00.000Z",
        ),
    }
    return a


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestProcessGroupMessages:
    """Test the message processing pipeline (trigger → agent → output)."""

    async def test_processes_triggered_message(self, app: PynchyApp, tmp_path: Path):
        """A triggered message should spawn a container and return the result."""
        msg = _make_message(content="@pynchy what is 2+2?")
        await store_message(msg)

        fake_proc = FakeProcess(output={
            "status": "success",
            "result": "The answer is 4",
            "new_session_id": "sess-1",
        })
        driver = asyncio.create_task(fake_proc.schedule_output())

        async def fake_create(*args: Any, **kwargs: Any) -> FakeProcess:
            return fake_proc

        channel = FakeChannel()
        app.channels = [channel]

        with (
            patch("pynchy.container_runner.asyncio.create_subprocess_exec", fake_create),
            patch("pynchy.container_runner.PROJECT_ROOT", tmp_path),
            patch("pynchy.container_runner.GROUPS_DIR", tmp_path / "groups"),
            patch("pynchy.container_runner.DATA_DIR", tmp_path / "data"),
        ):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)
            result = await app._process_group_messages("group@g.us")

        await driver
        assert result is True
        assert app.sessions.get("test-group") == "sess-1"
        # Output should have been sent via the channel
        assert len(channel.sent_messages) == 1
        assert "The answer is 4" in channel.sent_messages[0][1]

    async def test_skips_messages_without_trigger(self, app: PynchyApp, tmp_path: Path):
        """Messages without @pynchy trigger should be skipped for non-main groups."""
        msg = _make_message(content="just a regular message without trigger")
        await store_message(msg)

        result = await app._process_group_messages("group@g.us")
        assert result is True
        # No container should have been spawned (no trigger)

    async def test_rolls_back_cursor_on_error(self, app: PynchyApp, tmp_path: Path):
        """On agent error (before any output), cursor should roll back for retry."""
        msg = _make_message(content="@pynchy fail please")
        await store_message(msg)

        fake_proc = FakeProcess()
        # Simulate error exit
        async def schedule_error():
            await asyncio.sleep(0.01)
            fake_proc.stderr.feed_data(b"something broke\n")
            await asyncio.sleep(0.01)
            fake_proc._returncode = 1
            fake_proc.stdout.feed_eof()
            fake_proc.stderr.feed_eof()
            fake_proc._wait_event.set()

        driver = asyncio.create_task(schedule_error())

        async def fake_create(*args: Any, **kwargs: Any) -> FakeProcess:
            return fake_proc

        app.channels = [FakeChannel()]

        with (
            patch("pynchy.container_runner.asyncio.create_subprocess_exec", fake_create),
            patch("pynchy.container_runner.PROJECT_ROOT", tmp_path),
            patch("pynchy.container_runner.GROUPS_DIR", tmp_path / "groups"),
            patch("pynchy.container_runner.DATA_DIR", tmp_path / "data"),
        ):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)
            result = await app._process_group_messages("group@g.us")

        await driver
        assert result is False  # Error → should return False for retry
        # Cursor should NOT have been advanced (rolled back)
        assert app.last_agent_timestamp.get("group@g.us", "") == ""

    async def test_main_group_processes_without_trigger(self, app: PynchyApp, tmp_path: Path):
        """Main group doesn't require trigger — all messages are processed."""
        app.registered_groups = {
            "main@g.us": RegisteredGroup(
                name="Main",
                folder="main",
                trigger="always",
                added_at="2024-01-01T00:00:00.000Z",
            ),
        }
        msg = _make_message(chat_jid="main@g.us", content="no trigger needed")
        await store_message(msg)

        fake_proc = FakeProcess(output={
            "status": "success",
            "result": "Got it",
            "new_session_id": "s-main",
        })
        driver = asyncio.create_task(fake_proc.schedule_output())

        async def fake_create(*args: Any, **kwargs: Any) -> FakeProcess:
            return fake_proc

        app.channels = [FakeChannel()]

        with (
            patch("pynchy.container_runner.asyncio.create_subprocess_exec", fake_create),
            patch("pynchy.container_runner.PROJECT_ROOT", tmp_path),
            patch("pynchy.container_runner.GROUPS_DIR", tmp_path / "groups"),
            patch("pynchy.container_runner.DATA_DIR", tmp_path / "data"),
        ):
            (tmp_path / "groups" / "main").mkdir(parents=True)
            result = await app._process_group_messages("main@g.us")

        await driver
        assert result is True


class TestRunAgent:
    """Test the agent runner wrapper."""

    async def test_returns_success_on_good_output(self, app: PynchyApp, tmp_path: Path):
        fake_proc = FakeProcess(output={
            "status": "success",
            "result": "hello world",
            "new_session_id": "s-1",
        })
        driver = asyncio.create_task(fake_proc.schedule_output())

        async def fake_create(*args: Any, **kwargs: Any) -> FakeProcess:
            return fake_proc

        group = app.registered_groups["group@g.us"]

        with (
            patch("pynchy.container_runner.asyncio.create_subprocess_exec", fake_create),
            patch("pynchy.container_runner.PROJECT_ROOT", tmp_path),
            patch("pynchy.container_runner.GROUPS_DIR", tmp_path / "groups"),
            patch("pynchy.container_runner.DATA_DIR", tmp_path / "data"),
        ):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)
            result = await app._run_agent(group, "test prompt", "group@g.us")

        await driver
        assert result == "success"
        assert app.sessions.get("test-group") == "s-1"

    async def test_returns_error_on_exception(self, app: PynchyApp, tmp_path: Path):
        async def failing_create(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("spawn failed")

        group = app.registered_groups["group@g.us"]

        with (
            patch("pynchy.container_runner.asyncio.create_subprocess_exec", failing_create),
            patch("pynchy.container_runner.PROJECT_ROOT", tmp_path),
            patch("pynchy.container_runner.GROUPS_DIR", tmp_path / "groups"),
            patch("pynchy.container_runner.DATA_DIR", tmp_path / "data"),
        ):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)
            result = await app._run_agent(group, "test prompt", "group@g.us")

        assert result == "error"


class TestRecoverPendingMessages:
    """Test startup crash recovery."""

    async def test_enqueues_groups_with_pending_messages(self, app: PynchyApp):
        # Store a message but don't advance the cursor
        msg = _make_message(content="missed message")
        await store_message(msg)

        enqueued = []
        app.queue.enqueue_message_check = lambda jid: enqueued.append(jid)  # type: ignore[assignment]

        await app._recover_pending_messages()
        assert "group@g.us" in enqueued

    async def test_skips_groups_with_no_pending_messages(self, app: PynchyApp):
        # No messages stored at all
        enqueued = []
        app.queue.enqueue_message_check = lambda jid: enqueued.append(jid)  # type: ignore[assignment]

        await app._recover_pending_messages()
        assert len(enqueued) == 0


class TestStatePersistence:
    """Test state load/save round-trips."""

    async def test_save_and_load_state(self, app: PynchyApp):
        app.last_timestamp = "2024-06-01T12:00:00Z"
        app.last_agent_timestamp = {"group@g.us": "2024-06-01T11:00:00Z"}
        await app._save_state()

        # Create a new app and load state
        app2 = PynchyApp()
        await app2._load_state()
        assert app2.last_timestamp == "2024-06-01T12:00:00Z"
        assert app2.last_agent_timestamp == {"group@g.us": "2024-06-01T11:00:00Z"}

    async def test_load_state_handles_corrupted_json(self, app: PynchyApp):
        from pynchy.db import set_router_state
        await set_router_state("last_agent_timestamp", "not valid json")

        app2 = PynchyApp()
        await app2._load_state()
        # Should reset to empty dict, not crash
        assert app2.last_agent_timestamp == {}


class TestFindChannel:
    def test_finds_channel_that_owns_jid(self):
        app = PynchyApp()
        channel = FakeChannel()
        app.channels = [channel]
        assert app._find_channel("group@g.us") is channel

    def test_returns_none_for_unknown_jid(self):
        app = PynchyApp()
        app.channels = []
        assert app._find_channel("group@g.us") is None
