"""Broadcast consistency tests.

Verifies that BOTH channel sends AND EventBus emissions carry matching,
meaningful content for every trace event type. Catches divergence between
the channel path and the TUI/SSE path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from conftest import make_settings

from pynchy.app import PynchyApp
from pynchy.chat.router import format_tool_preview
from pynchy.db import _init_test_database, store_message
from pynchy.event_bus import AgentTraceEvent, MessageEvent
from pynchy.types import NewMessage, WorkspaceProfile

_CR_ORCH = "pynchy.container_runner._orchestrator"

# ---------------------------------------------------------------------------
# Helpers (shared patterns from test_app_integration.py)
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


async def _noop_docker_rm(name: str) -> None:
    """No-op replacement for _docker_rm_force in tests."""


@contextlib.contextmanager
def _patch_test_settings(tmp_path: Path):
    """Patch settings accessors and container helpers for test isolation."""
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
            "pynchy.container_runner._session",
            "pynchy.container_runner._snapshots",
            "pynchy.chat.message_handler",
            "pynchy.chat.output_handler",
        ):
            stack.enter_context(patch(f"{mod}.get_settings", return_value=s))
        stack.enter_context(
            patch("pynchy.container_runner._process._docker_rm_force", _noop_docker_rm)
        )
        stack.enter_context(
            patch("pynchy.container_runner._session._docker_rm_force", _noop_docker_rm)
        )
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

    def owns_jid(self, jid: str) -> bool:  # noqa: ARG002
        return True

    async def disconnect(self) -> None:
        self.connected = False


class FakeProcess:
    """Simulates asyncio.subprocess.Process for integration tests.

    Output is delivered via the session's public API (simulating the IPC
    watcher), not via stdout markers.
    """

    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self._returncode: int | None = None
        self._wait_event = asyncio.Event()
        self.pid = 12345

    def finish(self) -> None:
        self._returncode = 0
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


class EventCapture:
    """Captures EventBus emissions for assertions."""

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
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def app(tmp_path: Path):
    await _init_test_database()
    a = PynchyApp()
    a.workspaces = {
        "group@g.us": WorkspaceProfile(
            jid="group@g.us",
            name="Test Group",
            folder="test-group",
            trigger="@pynchy",
            added_at="2024-01-01T00:00:00.000Z",
        ),
    }
    yield a
    # Clean up any persistent sessions created during the test
    from pynchy.container_runner._session import destroy_all_sessions

    await destroy_all_sessions()


async def _run_with_trace_sequence(
    app: PynchyApp, tmp_path: Path, trace_outputs: list[dict[str, Any]]
) -> tuple[FakeChannel, EventCapture]:
    """Run app._process_group_messages with a sequence of trace outputs.

    Delivers output via the session's public API (simulating the IPC watcher).
    Returns (channel, event_capture) for assertions.
    """
    from pynchy.container_runner._process import is_query_done_pulse
    from pynchy.container_runner._serialization import _parse_container_output
    from pynchy.container_runner._session import get_session

    msg = _make_message(content="@pynchy do something")
    await store_message(msg)

    fake_proc = FakeProcess()

    async def schedule():
        # Wait for session to be created with an output handler
        for _ in range(100):
            session = get_session("test-group")
            if session is not None and session._on_output is not None:
                break
            await asyncio.sleep(0.01)
        assert session is not None, "No session found for test-group"

        for output_dict in trace_outputs:
            await asyncio.sleep(0.01)
            parsed = _parse_container_output(json.dumps(output_dict))
            if session._on_output:
                await session._on_output(parsed)
            if is_query_done_pulse(parsed):
                session.signal_query_done()

        # Append query-done pulse if not already signaled
        if not session._query_done.is_set():
            pulse = _parse_container_output(
                json.dumps({"status": "success", "result": None, "new_session_id": "test-session"})
            )
            if session._on_output:
                await session._on_output(pulse)
            session.signal_query_done()

        await asyncio.sleep(0.01)
        fake_proc.finish()

    driver = asyncio.create_task(schedule())

    async def fake_create(*args: Any, **kwargs: Any) -> FakeProcess:
        return fake_proc

    channel = FakeChannel()
    app.channels = [channel]
    capture = EventCapture(app.event_bus)

    with (
        patch(f"{_CR_ORCH}.asyncio.create_subprocess_exec", fake_create),
        _patch_test_settings(tmp_path),
    ):
        (tmp_path / "groups" / "test-group").mkdir(parents=True)
        await app._process_group_messages("group@g.us")

    await driver
    await capture.drain()
    return channel, capture


# ---------------------------------------------------------------------------
# Tests: format_tool_preview()
# ---------------------------------------------------------------------------


class TestFormatToolPreview:
    """Unit tests for the format_tool_preview helper."""

    def test_bash_shows_command(self):
        result = format_tool_preview("Bash", {"command": "ls -la /tmp"})
        assert "ls -la /tmp" in result
        assert "Bash" in result

    def test_bash_truncates_long_command(self):
        long_cmd = "find / -name '*.py' -exec grep -l 'import asyncio' {} + | " + "x" * 200
        result = format_tool_preview("Bash", {"command": long_cmd})
        assert len(result) < len(long_cmd) + 20  # name + truncated command
        assert "..." in result

    def test_bash_preserves_medium_command(self):
        """Commands under 180 chars should not be truncated."""
        cmd = "find / -name '*.py' -exec grep -l 'import asyncio' {} + | sort | uniq -c | sort -rn"
        result = format_tool_preview("Bash", {"command": cmd})
        assert result == f"Bash: {cmd}"
        assert "..." not in result

    def test_read_shows_file_path(self):
        result = format_tool_preview("Read", {"file_path": "/src/pynchy/app.py"})
        assert "app.py" in result

    def test_edit_shows_file_path(self):
        result = format_tool_preview("Edit", {"file_path": "/src/pynchy/router.py"})
        assert "router.py" in result

    def test_write_shows_file_path(self):
        result = format_tool_preview("Write", {"file_path": "/src/pynchy/new_file.py"})
        assert "new_file.py" in result

    def test_grep_shows_pattern_and_path(self):
        result = format_tool_preview("Grep", {"pattern": "TODO", "path": "/src"})
        assert "TODO" in result

    def test_glob_shows_pattern(self):
        result = format_tool_preview("Glob", {"pattern": "**/*.py"})
        assert "**/*.py" in result

    def test_unknown_tool_uses_fallback(self):
        result = format_tool_preview("CustomTool", {"key": "value"})
        assert "CustomTool" in result

    def test_empty_input(self):
        result = format_tool_preview("Bash", {})
        assert "Bash" in result


# ---------------------------------------------------------------------------
# Tests: Broadcast consistency
# ---------------------------------------------------------------------------


class TestBroadcastConsistency:
    """Verify that channels and EventBus receive matching content."""

    async def test_tool_use_channels_show_bash_command(self, app: PynchyApp, tmp_path: Path):
        """Bash tool_use should show the command in channel text, not just '🔧 Bash'."""
        channel, _ = await _run_with_trace_sequence(
            app,
            tmp_path,
            [
                {
                    "type": "tool_use",
                    "status": "success",
                    "tool_name": "Bash",
                    "tool_input": {"command": "git status"},
                },
                {
                    "type": "result",
                    "status": "success",
                    "result": "Done",
                    "new_session_id": "s1",
                },
            ],
        )
        tool_texts = [t for _, t in channel.sent_messages if "Bash" in t]
        assert tool_texts, "Expected a channel message mentioning Bash"
        # The channel text should include the actual command, not just the tool name
        assert any("git status" in t for t in tool_texts), (
            f"Expected 'git status' in channel tool_use text, got: {tool_texts}"
        )

    async def test_tool_use_channels_show_file_path(self, app: PynchyApp, tmp_path: Path):
        """Read/Edit tool_use should show the file path in channel text."""
        channel, _ = await _run_with_trace_sequence(
            app,
            tmp_path,
            [
                {
                    "type": "tool_use",
                    "status": "success",
                    "tool_name": "Read",
                    "tool_input": {"file_path": "/src/pynchy/app.py"},
                },
                {
                    "type": "result",
                    "status": "success",
                    "result": "Done",
                    "new_session_id": "s1",
                },
            ],
        )
        tool_texts = [t for _, t in channel.sent_messages if "Read" in t]
        assert tool_texts, "Expected a channel message mentioning Read"
        assert any("app.py" in t for t in tool_texts), (
            f"Expected 'app.py' in channel Read text, got: {tool_texts}"
        )

    async def test_tool_use_eventbus_receives_full_data(self, app: PynchyApp, tmp_path: Path):
        """EventBus should receive the full tool_input dict."""
        _, capture = await _run_with_trace_sequence(
            app,
            tmp_path,
            [
                {
                    "type": "tool_use",
                    "status": "success",
                    "tool_name": "Bash",
                    "tool_input": {"command": "echo hello"},
                },
                {
                    "type": "result",
                    "status": "success",
                    "result": "Done",
                    "new_session_id": "s1",
                },
            ],
        )
        tool_traces = [t for t in capture.traces if t.trace_type == "tool_use"]
        assert len(tool_traces) >= 1
        assert tool_traces[0].data["tool_name"] == "Bash"
        assert tool_traces[0].data["tool_input"] == {"command": "echo hello"}

    async def test_tool_use_eventbus_and_channels_both_receive(
        self, app: PynchyApp, tmp_path: Path
    ):
        """Both EventBus and channels must fire for every tool_use event."""
        channel, capture = await _run_with_trace_sequence(
            app,
            tmp_path,
            [
                {
                    "type": "tool_use",
                    "status": "success",
                    "tool_name": "Bash",
                    "tool_input": {"command": "ls"},
                },
                {
                    "type": "tool_use",
                    "status": "success",
                    "tool_name": "Read",
                    "tool_input": {"file_path": "/tmp/test.py"},
                },
                {
                    "type": "result",
                    "status": "success",
                    "result": "Done",
                    "new_session_id": "s1",
                },
            ],
        )
        # Both channels and EventBus should have 2 tool_use events
        tool_channel = [t for _, t in channel.sent_messages if "\U0001f527" in t]
        tool_traces = [t for t in capture.traces if t.trace_type == "tool_use"]
        assert len(tool_channel) == 2, f"Expected 2 channel tool msgs, got {len(tool_channel)}"
        assert len(tool_traces) == 2, f"Expected 2 EventBus tool traces, got {len(tool_traces)}"

    async def test_direct_command_shows_output(self, app: PynchyApp, tmp_path: Path):
        """!command output should reach both channels and EventBus with actual stdout."""
        msg = _make_message(content="!echo hello world")
        await store_message(msg)

        channel = FakeChannel()
        app.channels = [channel]
        capture = EventCapture(app.event_bus)

        group = app.workspaces["group@g.us"]

        with _patch_test_settings(tmp_path):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)
            from pynchy.chat import message_handler

            await message_handler.execute_direct_command(
                app, "group@g.us", group, msg, "echo hello world"
            )

        await capture.drain()

        # Channel should have the command output with actual content
        channel_texts = [t for _, t in channel.sent_messages]
        assert any("hello world" in t for t in channel_texts), (
            f"Expected 'hello world' in channel output, got: {channel_texts}"
        )

        # EventBus should also receive the output
        assert len(capture.messages) >= 1, "EventBus should receive MessageEvent for direct command"
        assert any("hello world" in m.content for m in capture.messages)

    async def test_sse_bridge_propagates_traces(self, app: PynchyApp, tmp_path: Path):
        """subscribe_events() callback should receive tool_use dicts (end-to-end TUI path)."""
        received: list[dict[str, Any]] = []

        async def sse_callback(data: dict[str, Any]) -> None:
            received.append(data)

        # Wire up the SSE bridge like the HTTP server does
        from pynchy.dep_factory import make_http_deps

        http_deps = make_http_deps(app)
        unsub = http_deps.subscribe_events(sse_callback)

        try:
            _, _ = await _run_with_trace_sequence(
                app,
                tmp_path,
                [
                    {
                        "type": "tool_use",
                        "status": "success",
                        "tool_name": "Bash",
                        "tool_input": {"command": "date"},
                    },
                    {
                        "type": "result",
                        "status": "success",
                        "result": "All done",
                        "new_session_id": "s1",
                    },
                ],
            )

            await asyncio.sleep(0.1)

            trace_events = [e for e in received if e.get("type") == "agent_trace"]
            tool_events = [e for e in trace_events if e.get("trace_type") == "tool_use"]
            assert len(tool_events) >= 1, (
                f"Expected tool_use in SSE events, got trace types: "
                f"{[e.get('trace_type') for e in trace_events]}"
            )
            assert tool_events[0]["tool_name"] == "Bash"
            assert tool_events[0]["tool_input"] == {"command": "date"}
        finally:
            unsub()


# ---------------------------------------------------------------------------
# Tests: User message broadcast consistency
# ---------------------------------------------------------------------------


class TestUserMessageBroadcast:
    """Verify that user messages from any UI are broadcast to all channels."""

    async def test_tui_message_broadcasts_to_channels(self, app: PynchyApp):
        """TUI user messages should be stored, emitted to event bus, AND broadcast to channels."""
        channel = FakeChannel()
        app.channels = [channel]
        capture = EventCapture(app.event_bus)

        # Get the HTTP deps (which includes send_user_message)
        from pynchy.dep_factory import make_http_deps

        http_deps = make_http_deps(app)

        # Simulate a TUI user sending a message
        await http_deps.send_user_message("group@g.us", "Hello from TUI")
        await capture.drain()

        # 1. Message should be stored in DB (already tested by other tests)
        # 2. EventBus should receive the message
        assert len(capture.messages) == 1
        assert capture.messages[0].content == "Hello from TUI"
        assert capture.messages[0].sender_name == "You"
        assert capture.messages[0].is_bot is False

        # 3. Message should be broadcast to channel
        assert len(channel.sent_messages) == 1
        sent_jid, sent_text = channel.sent_messages[0]
        assert sent_jid == "group@g.us"
        assert "Hello from TUI" in sent_text

    async def test_inbound_message_broadcasts_to_other_channels(self, app: PynchyApp):
        """Inbound messages from one channel should be broadcast to other channels."""
        # Create two channels: source and target
        source_channel = FakeChannel()
        source_channel.name = "source"
        target_channel = FakeChannel()
        target_channel.name = "target"

        app.channels = [source_channel, target_channel]
        capture = EventCapture(app.event_bus)

        # Simulate an inbound message from the source channel
        msg = _make_message(content="Hello from source")
        await app._on_inbound("group@g.us", msg)
        await capture.drain()

        # 1. EventBus should receive the message
        assert len(capture.messages) == 1
        assert capture.messages[0].content == "Hello from source"
        assert capture.messages[0].is_bot is False

        # 2. Message should be broadcast to OTHER channels (not back to source)
        # Currently this FAILS because _on_inbound doesn't broadcast
        sent_to_target = [m for m in target_channel.sent_messages if "Hello from source" in m[1]]
        assert len(sent_to_target) == 1, "User messages should be broadcast to other channels"
