"""Integration tests for PynchyApp.

End-to-end tests that wire up real subsystems (DB, queue, message processing)
with mocked boundaries (WhatsApp channel, container subprocess, Apple Container CLI).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_settings

from pynchy.app import PynchyApp
from pynchy.config import Settings
from pynchy.db import _init_test_database, get_chat_history, store_message
from pynchy.types import NewMessage, RegisteredGroup

_CR_ORCH = "pynchy.container_runner._orchestrator"

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
    payload = (
        f"{Settings.OUTPUT_START_MARKER}\n{json.dumps(output)}\n{Settings.OUTPUT_END_MARKER}\n"
    )
    return payload.encode()


@contextlib.contextmanager
def _patch_test_settings(tmp_path: Path):
    """Patch settings accessors to use tmp test directories."""
    s = make_settings(
        project_root=tmp_path,
        groups_dir=tmp_path / "groups",
        data_dir=tmp_path / "data",
    )
    with contextlib.ExitStack() as stack:
        for mod in (
            "pynchy.container_runner._credentials",
            "pynchy.container_runner._mounts",
            "pynchy.container_runner._session_prep",
            "pynchy.container_runner._orchestrator",
            "pynchy.container_runner._snapshots",
            "pynchy.messaging.message_handler",
            "pynchy.messaging.output_handler",
        ):
            stack.enter_context(patch(f"{mod}.get_settings", return_value=s))
        yield


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


class TestAppImports:
    """Verify lazy imports in app.run() resolve correctly."""

    def test_channel_runtime_import(self):
        """Channel runtime helper import in app.run() must resolve."""
        from pynchy.messaging.channel_runtime import ChannelPluginContext  # noqa: F401


class TestFirstRunBootstrap:
    """Verify first-run workspace bootstrap without external channels."""

    async def test_creates_tui_god_workspace_without_channel(self, app: PynchyApp):
        app.registered_groups = {}
        app.workspaces = {}

        from pynchy import startup_handler

        await startup_handler.setup_god_group(app, default_channel=None)

        assert len(app.registered_groups) == 1
        [(jid, group)] = list(app.registered_groups.items())
        assert jid.startswith("tui://")
        assert group.is_god is True


class TestProcessGroupMessages:
    """Test the message processing pipeline (trigger → agent → output)."""

    async def test_processes_triggered_message(self, app: PynchyApp, tmp_path: Path):
        """A triggered message should spawn a container and return the result."""
        msg = _make_message(content="@pynchy what is 2+2?")
        await store_message(msg)

        fake_proc = FakeProcess(
            output={
                "status": "success",
                "result": "The answer is 4",
                "new_session_id": "sess-1",
            }
        )
        driver = asyncio.create_task(fake_proc.schedule_output())

        async def fake_create(*args: Any, **kwargs: Any) -> FakeProcess:
            return fake_proc

        channel = FakeChannel()
        app.channels = [channel]

        with (
            patch(f"{_CR_ORCH}.asyncio.create_subprocess_exec", fake_create),
            _patch_test_settings(tmp_path),
        ):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)
            result = await app._process_group_messages("group@g.us")

        await driver
        assert result is True
        assert app.sessions.get("test-group") == "sess-1"
        # Output should have been sent via the channel
        assert len(channel.sent_messages) == 1
        assert "The answer is 4" in channel.sent_messages[0][1]

    async def test_trace_events_forwarded_to_channels(self, app: PynchyApp, tmp_path: Path):
        """Thinking and tool_use trace events should be sent to channels, not just results."""
        msg = _make_message(content="@pynchy do something complex")
        await store_message(msg)

        # Simulate a realistic agent session: thinking → tool_use → result
        fake_proc = FakeProcess()
        driver_started = asyncio.Event()

        async def schedule_trace_sequence():
            driver_started.set()
            await asyncio.sleep(0.01)
            # 1. Thinking block
            fake_proc.stdout.feed_data(
                _marker_wrap(
                    {
                        "type": "thinking",
                        "status": "success",
                        "thinking": "Let me figure this out...",
                    }
                )
            )
            await asyncio.sleep(0.01)
            # 2. Tool use block
            fake_proc.stdout.feed_data(
                _marker_wrap(
                    {
                        "type": "tool_use",
                        "status": "success",
                        "tool_name": "Bash",
                        "tool_input": {"command": "ls"},
                    }
                )
            )
            await asyncio.sleep(0.01)
            # 3. Final result
            fake_proc.stdout.feed_data(
                _marker_wrap(
                    {
                        "type": "result",
                        "status": "success",
                        "result": "Done!",
                        "new_session_id": "sess-trace",
                    }
                )
            )
            await asyncio.sleep(0.01)
            fake_proc._returncode = 0
            fake_proc.stdout.feed_eof()
            fake_proc.stderr.feed_eof()
            fake_proc._wait_event.set()

        driver = asyncio.create_task(schedule_trace_sequence())

        async def fake_create(*args: Any, **kwargs: Any) -> FakeProcess:
            return fake_proc

        channel = FakeChannel()
        app.channels = [channel]

        with (
            patch(f"{_CR_ORCH}.asyncio.create_subprocess_exec", fake_create),
            _patch_test_settings(tmp_path),
        ):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)
            result = await app._process_group_messages("group@g.us")

        await driver
        assert result is True

        # Extract just the message texts
        texts = [text for _, text in channel.sent_messages]

        # Trace events should have been sent BEFORE the final result
        assert any("thinking" in t.lower() for t in texts), (
            f"Expected a thinking trace message, got: {texts}"
        )
        assert any("Bash" in t for t in texts), (
            f"Expected a tool_use trace for 'Bash', got: {texts}"
        )
        # Final result should also be present
        assert any("Done!" in t for t in texts), f"Expected final result 'Done!', got: {texts}"
        # Thinking and tool traces should come before the result
        thinking_idx = next(i for i, t in enumerate(texts) if "thinking" in t.lower())
        tool_idx = next(i for i, t in enumerate(texts) if "Bash" in t)
        result_idx = next(i for i, t in enumerate(texts) if "Done!" in t)
        assert thinking_idx < result_idx, "Thinking trace should come before result"
        assert tool_idx < result_idx, "Tool trace should come before result"

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
            patch(f"{_CR_ORCH}.asyncio.create_subprocess_exec", fake_create),
            _patch_test_settings(tmp_path),
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

        fake_proc = FakeProcess(
            output={
                "status": "success",
                "result": "Got it",
                "new_session_id": "s-main",
            }
        )
        driver = asyncio.create_task(fake_proc.schedule_output())

        async def fake_create(*args: Any, **kwargs: Any) -> FakeProcess:
            return fake_proc

        app.channels = [FakeChannel()]

        worktree_path = tmp_path / "worktrees" / "main"
        worktree_path.mkdir(parents=True)
        fake_wt = MagicMock()
        fake_wt.path = worktree_path
        fake_wt.notices = []

        with (
            patch(f"{_CR_ORCH}.asyncio.create_subprocess_exec", fake_create),
            _patch_test_settings(tmp_path),
            patch("pynchy.git_ops.worktree.ensure_worktree", return_value=fake_wt),
        ):
            (tmp_path / "groups" / "main").mkdir(parents=True)
            result = await app._process_group_messages("main@g.us")

        await driver
        assert result is True


class TestRunAgent:
    """Test the agent runner wrapper."""

    async def test_returns_success_on_good_output(self, app: PynchyApp, tmp_path: Path):
        fake_proc = FakeProcess(
            output={
                "status": "success",
                "result": "hello world",
                "new_session_id": "s-1",
            }
        )
        driver = asyncio.create_task(fake_proc.schedule_output())

        async def fake_create(*args: Any, **kwargs: Any) -> FakeProcess:
            return fake_proc

        group = app.registered_groups["group@g.us"]

        with (
            patch(f"{_CR_ORCH}.asyncio.create_subprocess_exec", fake_create),
            _patch_test_settings(tmp_path),
        ):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)
            result = await app.run_agent(group, "test prompt", "group@g.us")

        await driver
        assert result == "success"
        assert app.sessions.get("test-group") == "s-1"

    async def test_returns_error_on_exception(self, app: PynchyApp, tmp_path: Path):
        async def failing_create(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("spawn failed")

        group = app.registered_groups["group@g.us"]

        with (
            patch(f"{_CR_ORCH}.asyncio.create_subprocess_exec", failing_create),
            _patch_test_settings(tmp_path),
        ):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)
            result = await app.run_agent(group, "test prompt", "group@g.us")

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


class TestTracePersistence:
    """Verify that trace events (thinking, tool_use, system, result_meta) are
    persisted to the database with correct sender values."""

    async def test_thinking_and_tool_use_persisted(self, app: PynchyApp, tmp_path: Path):
        """Thinking and tool_use events should be stored in DB."""
        msg = _make_message(content="@pynchy do something")
        await store_message(msg)

        fake_proc = FakeProcess()

        async def schedule_trace():
            await asyncio.sleep(0.01)
            fake_proc.stdout.feed_data(
                _marker_wrap(
                    {
                        "type": "thinking",
                        "status": "success",
                        "thinking": "Let me think...",
                    }
                )
            )
            await asyncio.sleep(0.01)
            fake_proc.stdout.feed_data(
                _marker_wrap(
                    {
                        "type": "tool_use",
                        "status": "success",
                        "tool_name": "Bash",
                        "tool_input": {"command": "echo hi"},
                    }
                )
            )
            await asyncio.sleep(0.01)
            fake_proc.stdout.feed_data(
                _marker_wrap(
                    {
                        "type": "result",
                        "status": "success",
                        "result": "Done",
                        "new_session_id": "sess-trace",
                    }
                )
            )
            await asyncio.sleep(0.01)
            fake_proc._returncode = 0
            fake_proc.stdout.feed_eof()
            fake_proc.stderr.feed_eof()
            fake_proc._wait_event.set()

        driver = asyncio.create_task(schedule_trace())

        async def fake_create(*args: Any, **kwargs: Any) -> FakeProcess:
            return fake_proc

        channel = FakeChannel()
        app.channels = [channel]

        with (
            patch(f"{_CR_ORCH}.asyncio.create_subprocess_exec", fake_create),
            _patch_test_settings(tmp_path),
        ):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)
            await app._process_group_messages("group@g.us")

        await driver

        # Check DB for persisted trace messages
        history = await get_chat_history("group@g.us", limit=50)
        senders = {m.sender for m in history}
        assert "thinking" in senders, f"Expected 'thinking' in senders, got {senders}"
        assert "tool_use" in senders, f"Expected 'tool_use' in senders, got {senders}"
        assert "bot" in senders, f"Expected 'bot' in senders, got {senders}"

    async def test_system_message_persisted(self, app: PynchyApp, tmp_path: Path):
        """System messages should be stored with sender='system'."""
        msg = _make_message(content="@pynchy hello")
        await store_message(msg)

        fake_proc = FakeProcess()

        async def schedule_system():
            await asyncio.sleep(0.01)
            fake_proc.stdout.feed_data(
                _marker_wrap(
                    {
                        "type": "system",
                        "status": "success",
                        "system_subtype": "init",
                        "system_data": {"session_id": "sess-sys"},
                    }
                )
            )
            await asyncio.sleep(0.01)
            fake_proc.stdout.feed_data(
                _marker_wrap(
                    {
                        "type": "result",
                        "status": "success",
                        "result": "Hi",
                        "new_session_id": "sess-sys",
                    }
                )
            )
            await asyncio.sleep(0.01)
            fake_proc._returncode = 0
            fake_proc.stdout.feed_eof()
            fake_proc.stderr.feed_eof()
            fake_proc._wait_event.set()

        driver = asyncio.create_task(schedule_system())

        async def fake_create(*args: Any, **kwargs: Any) -> FakeProcess:
            return fake_proc

        channel = FakeChannel()
        app.channels = [channel]

        with (
            patch(f"{_CR_ORCH}.asyncio.create_subprocess_exec", fake_create),
            _patch_test_settings(tmp_path),
        ):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)
            await app._process_group_messages("group@g.us")

        await driver

        history = await get_chat_history("group@g.us", limit=50)
        system_msgs = [m for m in history if m.sender == "system"]
        assert len(system_msgs) >= 1
        content = json.loads(system_msgs[0].content)
        assert content["subtype"] == "init"

    async def test_result_metadata_persisted(self, app: PynchyApp, tmp_path: Path):
        """Result metadata should be stored with sender='result_meta'."""
        msg = _make_message(content="@pynchy hello")
        await store_message(msg)

        fake_proc = FakeProcess()

        async def schedule_meta():
            await asyncio.sleep(0.01)
            fake_proc.stdout.feed_data(
                _marker_wrap(
                    {
                        "type": "result",
                        "status": "success",
                        "result": "Hi",
                        "new_session_id": "sess-meta",
                        "result_metadata": {
                            "duration_ms": 2100,
                            "total_cost_usd": 0.03,
                            "num_turns": 3,
                            "usage": {"input_tokens": 100, "output_tokens": 50},
                        },
                    }
                )
            )
            await asyncio.sleep(0.01)
            fake_proc._returncode = 0
            fake_proc.stdout.feed_eof()
            fake_proc.stderr.feed_eof()
            fake_proc._wait_event.set()

        driver = asyncio.create_task(schedule_meta())

        async def fake_create(*args: Any, **kwargs: Any) -> FakeProcess:
            return fake_proc

        channel = FakeChannel()
        app.channels = [channel]

        with (
            patch(f"{_CR_ORCH}.asyncio.create_subprocess_exec", fake_create),
            _patch_test_settings(tmp_path),
        ):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)
            await app._process_group_messages("group@g.us")

        await driver

        history = await get_chat_history("group@g.us", limit=50)
        meta_msgs = [m for m in history if m.sender == "result_meta"]
        assert len(meta_msgs) >= 1
        content = json.loads(meta_msgs[0].content)
        assert content["total_cost_usd"] == 0.03
        assert content["num_turns"] == 3

        # Channel should have received the formatted cost message
        texts = [text for _, text in channel.sent_messages]
        assert any("0.03 USD" in t for t in texts), f"Expected cost in channel, got {texts}"


class TestDeployContinuationResume:
    """Verify multi-group resume after deploy restart."""

    async def test_resumes_all_groups_from_active_sessions(self, app: PynchyApp, tmp_path: Path):
        """check_deploy_continuation should inject resume messages for every active session."""
        await _init_test_database()

        # Register two groups
        app.registered_groups = {
            "god@g.us": RegisteredGroup(
                name="God",
                folder="god",
                trigger="always",
                added_at="2024-01-01T00:00:00.000Z",
                is_god=True,
            ),
            "team@g.us": RegisteredGroup(
                name="Team",
                folder="team",
                trigger="@pynchy",
                added_at="2024-01-01T00:00:00.000Z",
            ),
        }

        # Write a continuation file with active_sessions for both groups
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        continuation = {
            "chat_jid": "god@g.us",
            "session_id": "sess-god",
            "resume_prompt": "Deploy complete.",
            "commit_sha": "abc12345",
            "previous_commit_sha": "000",
            "active_sessions": {
                "god@g.us": "sess-god",
                "team@g.us": "sess-team",
            },
        }
        (data_dir / "deploy_continuation.json").write_text(json.dumps(continuation))

        enqueued: list[str] = []
        app.queue.enqueue_message_check = lambda jid: enqueued.append(jid)  # type: ignore[assignment]

        with patch("pynchy.startup_handler.get_settings") as mock_settings:
            s = MagicMock()
            s.data_dir = data_dir
            mock_settings.return_value = s
            from pynchy.startup_handler import check_deploy_continuation

            await check_deploy_continuation(app)

        # Both groups should have been enqueued for resume
        assert "god@g.us" in enqueued
        assert "team@g.us" in enqueued

        # Both groups should have a deploy resume message in history
        god_history = await get_chat_history("god@g.us", limit=10)
        team_history = await get_chat_history("team@g.us", limit=10)
        assert any("DEPLOY COMPLETE" in m.content for m in god_history)
        assert any("DEPLOY COMPLETE" in m.content for m in team_history)

        # Continuation file should be deleted
        assert not (data_dir / "deploy_continuation.json").exists()

    async def test_backward_compat_single_session(self, app: PynchyApp, tmp_path: Path):
        """Old continuation files without active_sessions should still resume the single group."""
        await _init_test_database()

        app.registered_groups = {
            "god@g.us": RegisteredGroup(
                name="God",
                folder="god",
                trigger="always",
                added_at="2024-01-01T00:00:00.000Z",
                is_god=True,
            ),
        }

        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        # Old-style continuation: no active_sessions key
        continuation = {
            "chat_jid": "god@g.us",
            "session_id": "sess-god",
            "resume_prompt": "Deploy complete.",
            "commit_sha": "abc12345",
            "previous_commit_sha": "000",
        }
        (data_dir / "deploy_continuation.json").write_text(json.dumps(continuation))

        enqueued: list[str] = []
        app.queue.enqueue_message_check = lambda jid: enqueued.append(jid)  # type: ignore[assignment]

        with patch("pynchy.startup_handler.get_settings") as mock_settings:
            s = MagicMock()
            s.data_dir = data_dir
            mock_settings.return_value = s
            from pynchy.startup_handler import check_deploy_continuation

            await check_deploy_continuation(app)

        assert "god@g.us" in enqueued

    async def test_skips_when_no_active_sessions(self, app: PynchyApp, tmp_path: Path):
        """Continuation with empty active_sessions and no session_id should skip resume."""
        await _init_test_database()

        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        continuation = {
            "chat_jid": "god@g.us",
            "session_id": "",
            "resume_prompt": "Deploy complete.",
            "commit_sha": "abc12345",
            "previous_commit_sha": "000",
            "active_sessions": {},
        }
        (data_dir / "deploy_continuation.json").write_text(json.dumps(continuation))

        enqueued: list[str] = []
        app.queue.enqueue_message_check = lambda jid: enqueued.append(jid)  # type: ignore[assignment]

        with patch("pynchy.startup_handler.get_settings") as mock_settings:
            s = MagicMock()
            s.data_dir = data_dir
            mock_settings.return_value = s
            from pynchy.startup_handler import check_deploy_continuation

            await check_deploy_continuation(app)

        assert len(enqueued) == 0
