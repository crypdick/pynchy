"""Integration tests for PynchyApp.

End-to-end tests that wire up real subsystems (DB, queue, message processing)
with mocked boundaries (WhatsApp channel, container subprocess, Apple Container CLI).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import NullChannel, make_settings

from pynchy import state
from pynchy.config.models import NotificationsConfig
from pynchy.host.container_manager import serialization
from pynchy.host.container_manager.process import is_query_done_pulse
from pynchy.host.container_manager.session import destroy_all_sessions, get_session
from pynchy.host.orchestrator import startup_handler
from pynchy.host.orchestrator.app import PynchyApp
from pynchy.host.orchestrator.dep_factory import make_ipc_deps
from pynchy.host.orchestrator.execution_outcomes import TurnOutcome
from pynchy.host.orchestrator.runtime_target import RuntimeTarget
from pynchy.host.orchestrator.startup_handler import check_deploy_continuation
from pynchy.plugins.channel_runtime import ChannelPluginContext
from pynchy.state import get_chat_history, set_router_state, store_message
from pynchy.types import (
    AgentExecutionRuntime,
    DeployRevision,
    InFlightTurn,
    InFlightWorkKind,
    NewMessage,
    WorkspaceProfile,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from pathlib import Path

_CR_ORCH = "pynchy.host.container_manager.orchestrator"

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


def _completed_awaitable(value: Any = None) -> Awaitable[Any]:
    async def _completed() -> Any:
        await asyncio.sleep(0)
        return value

    return _completed()


def _failed_awaitable(exc: Exception) -> Awaitable[Any]:
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    future.set_exception(exc)
    return future


def _noop_docker_rm(name: str) -> Awaitable[None]:
    """No-op replacement for docker_rm_force in tests.

    docker_rm_force spawns a real subprocess (``container rm -f``) which
    hangs in the test environment where there is no container runtime.
    """
    return _completed_awaitable()


async def _seed_message(app: PynchyApp, msg: NewMessage) -> None:
    del app
    await store_message(
        NewMessage(
            id=msg.id,
            chat_jid=msg.chat_jid,
            sender=msg.sender,
            sender_name=msg.sender_name,
            content=msg.content,
            timestamp=msg.timestamp,
            is_from_me=msg.is_from_me,
            message_type=msg.message_type,
            metadata={"source": "test", **(msg.metadata or {})},
        ),
        message_type=msg.message_type or "user",
    )


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
            "pynchy.host.container_manager.credentials",
            "pynchy.host.learning.skill_activation",
            "pynchy.host.orchestrator.messaging.pipeline",
            "pynchy.host.orchestrator.messaging.router",
        ):
            stack.enter_context(patch(f"{mod}.get_settings", return_value=s))
        # Patch docker_rm_force which spawns a real subprocess to remove
        # containers — would hang in the test environment.  Must patch at both
        # the canonical location (process) and the import site (session)
        # because Python's from-import creates a separate reference.
        stack.enter_context(
            patch("pynchy.host.container_manager.process.docker_rm_force", _noop_docker_rm)
        )
        stack.enter_context(
            patch("pynchy.host.container_manager.session.docker_rm_force", _noop_docker_rm)
        )
        yield stack.enter_context(patch(f"{_CR_ORCH}.system_checks.ensure_agent_image_available"))


class FakeChannel(NullChannel):
    """Minimal Channel implementation for testing."""

    def __init__(self) -> None:
        self.name = "test"
        self.connected = True
        self.sent_messages: list[tuple[str, str]] = []

    async def connect(self) -> None:
        self.connected = True

    async def send_event(self, jid: str, event: object) -> None:
        content = event.content if hasattr(event, "content") else str(event)
        self.sent_messages.append((jid, content))

    def is_connected(self) -> bool:
        return self.connected

    def owns_jid(self, jid: str) -> bool:
        return True

    async def disconnect(self) -> None:
        self.connected = False


class FakeProcess(asyncio.subprocess.Process):
    """Simulates asyncio.subprocess.Process for integration tests.

    Output is delivered via the session's public API (simulating what the IPC
    watcher does), not via stdout markers.  The stdout stream is kept as a
    StreamReader for compatibility with session.start() but is never fed data.
    """

    def __init__(
        self,
        output: dict[str, Any] | None = None,
        group_folder: str = "test-group",
    ) -> None:
        self.stdin = FakeStdin()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self._returncode: int | None = None
        self._wait_event = asyncio.Event()
        self.pid = 12345
        self._output = output
        self._group_folder = group_folder

    async def schedule_output(self) -> None:
        """Deliver output via the session's output handler, then exit.

        Waits for the session to be created, dispatches the content output
        and query-done pulse through the session API (mirroring the IPC
        watcher's behavior), then simulates a clean process exit.
        """
        # Wait for the session to be created and have an output handler
        session = None
        for _ in range(200):
            session = get_session(self._group_folder)
            if session is not None and session.output_handler is not None:
                break
            await asyncio.sleep(0.01)

        assert session is not None, f"No session found for {self._group_folder}"
        handler = session.output_handler
        assert handler is not None, "Session has no output handler"

        if self._output:
            output = serialization.parse_container_output(json.dumps(self._output))
            await handler(output)

            # Emit query-done pulse via signal_query_done
            pulse_data = {
                "status": "success",
                "result": None,
                "new_session_id": self._output.get("new_session_id", "test-session"),
            }
            pulse = serialization.parse_container_output(json.dumps(pulse_data))
            await handler(pulse)
            session.signal_query_done()

        await asyncio.sleep(0.01)
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


class FakeStdin:
    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data += data

    def close(self) -> None:
        self.closed = True


async def _schedule_outputs_via_session(
    fake_proc: FakeProcess,
    outputs: list[dict[str, Any]],
    *,
    group_folder: str = "test-group",
    final_session_id: str = "test-session",
) -> None:
    """Deliver a sequence of outputs via the session's output handler.

    Simulates the IPC watcher's behavior: waits for the session to be created,
    dispatches each output dict through the handler, then signals query done
    and simulates a clean process exit.

    The last output in the list should be the final result (with new_session_id)
    that triggers query completion.  If no output has new_session_id, a
    query-done pulse is appended automatically.
    """
    # Wait for session to have an output handler
    for _ in range(100):
        session = get_session(group_folder)
        if session is not None and session.output_handler is not None:
            break
        await asyncio.sleep(0.01)

    assert session is not None, f"No session found for {group_folder}"
    handler = session.output_handler
    assert handler is not None, "Session has no output handler"
    emitted_pulse = False

    for output_dict in outputs:
        await asyncio.sleep(0.01)
        parsed = serialization.parse_container_output(json.dumps(output_dict))
        await handler(parsed)
        if is_query_done_pulse(parsed):
            emitted_pulse = True
            session.signal_query_done()

    # If no output triggered query done, append a pulse
    if not emitted_pulse:
        pulse = serialization.parse_container_output(
            json.dumps({"status": "success", "result": None, "new_session_id": final_session_id})
        )
        await handler(pulse)
        session.signal_query_done()

    await asyncio.sleep(0.01)
    fake_proc._returncode = 0
    fake_proc.stderr.feed_eof()
    fake_proc._wait_event.set()


def _trace_session_outputs() -> list[dict[str, Any]]:
    return [
        {"type": "thinking", "status": "success", "thinking": "Let me figure this out..."},
        {
            "type": "tool_use",
            "status": "success",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
        },
        {
            "type": "result",
            "status": "success",
            "result": "Done!",
            "new_session_id": "sess-trace",
        },
        {"status": "success", "result": None, "new_session_id": "sess-trace"},
    ]


def _sent_texts(channel: FakeChannel) -> list[str]:
    return [text for _, text in channel.sent_messages]


def _first_text_index(texts: list[str], needle: str) -> int:
    return next(index for index, text in enumerate(texts) if needle in text)


def _assert_trace_order(texts: list[str]) -> None:
    assert any("Let me figure this out" in text for text in texts), (
        f"Expected a thinking trace message, got: {texts}"
    )
    assert any("Bash" in text for text in texts), (
        f"Expected a tool_use trace for 'Bash', got: {texts}"
    )
    assert any("Done!" in text for text in texts), f"Expected final result 'Done!', got: {texts}"

    result_idx = _first_text_index(texts, "Done!")
    assert _first_text_index(texts, "Let me figure this out") < result_idx, (
        "Thinking trace should come before result"
    )
    assert _first_text_index(texts, "Bash") < result_idx, "Tool trace should come before result"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def app(tmp_path: Path):
    """Create a PynchyApp with a fresh in-memory DB and patched dirs."""
    await state.init_test_database()
    a = PynchyApp()
    a.agent_execution_runtime = AgentExecutionRuntime(
        project_root=tmp_path,
        groups_dir=tmp_path / "groups",
        data_dir=tmp_path / "data",
        mount_allowlist_path=a.agent_execution_runtime.mount_allowlist_path,
        blocked_mount_patterns=a.agent_execution_runtime.blocked_mount_patterns,
        agent_image=a.agent_execution_runtime.agent_image,
        agent_memory_mb=a.agent_execution_runtime.agent_memory_mb,
        container_timeout=a.agent_execution_runtime.container_timeout,
        default_core=a.agent_execution_runtime.default_core,
        idle_timeout=a.agent_execution_runtime.idle_timeout,
        model=a.agent_execution_runtime.model,
        model_reasoning_effort=a.agent_execution_runtime.model_reasoning_effort,
    )
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
    await destroy_all_sessions()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAppImports:
    """Verify lazy imports in app.run() resolve correctly."""

    def test_channel_runtime_import(self):
        """Channel runtime helper import in app.run() must resolve."""
        assert ChannelPluginContext is not None


async def test_ipc_context_reset_uses_canonical_lifecycle(app: PynchyApp) -> None:
    app.sessions["test-group"] = "session-before-reset"

    with (
        patch.object(app, "prepare_context_reset", new_callable=AsyncMock) as prepare,
        patch(
            "pynchy.host.orchestrator.session_handler.destroy_session",
            new_callable=AsyncMock,
        ) as destroy,
        patch(
            "pynchy.host.orchestrator.session_handler.clear_session",
            new_callable=AsyncMock,
        ) as clear,
    ):
        await make_ipc_deps(app).clear_session("test-group")

    prepare.assert_awaited_once_with(app.workspaces["group@g.us"])
    destroy.assert_awaited_once_with("test-group")
    clear.assert_awaited_once_with("test-group")
    assert "test-group" not in app.sessions
    assert "test-group" in app.session_cleared


async def test_ipc_deploy_resolves_missing_notification_target(app: PynchyApp) -> None:
    app.workspaces["group@g.us"].is_admin = True
    with (
        patch(
            "pynchy.host.orchestrator.dep_factory.get_settings",
            return_value=make_settings(
                notifications=NotificationsConfig(admin_workspace="test-group")
            ),
        ),
        patch(
            "pynchy.host.orchestrator.dep_factory.get_deploy_config_hash",
            return_value="config-hash",
        ),
        patch(
            "pynchy.host.orchestrator.dep_factory.start_deploy_workflow",
            new_callable=AsyncMock,
        ) as start_deploy,
    ):
        await make_ipc_deps(app).request_deploy(
            chat_jid=None,
            commit_sha="abc123",
            rebuild=False,
            resume_prompt="Done.",
        )

    request = start_deploy.await_args.args[0]
    assert request.chat_jid == "group@g.us"
    assert request.commit_sha == request.previous_sha == "abc123"
    assert request.config_hash == "config-hash"
    assert request.resume_prompt == "Done."


class TestFirstRunBootstrap:
    """Verify first-run workspace bootstrap requires a real channel."""

    async def test_creates_admin_workspace_through_command_center(self, app: PynchyApp):
        app.workspaces = {}
        channel = MagicMock(name="command_center")
        channel.name = "discord-primary"
        channel.create_group = AsyncMock(return_value="discord:channel:admin")

        await startup_handler.setup_admin_group(app, default_channel=channel)

        channel.create_group.assert_awaited_once_with("Pynchy")
        assert app.workspaces["discord:channel:admin"].is_admin is True

    async def test_rejects_bootstrap_without_creation_capable_channel(self, app: PynchyApp):
        app.workspaces = {}

        with pytest.raises(RuntimeError, match=r"command_center\.connection"):
            await startup_handler.setup_admin_group(app, default_channel=None)


class TestProcessGroupMessages:
    """Test the message processing pipeline (trigger → agent → output)."""

    async def test_processes_triggered_message(self, app: PynchyApp, tmp_path: Path):
        """A triggered message should spawn a container and return the result."""
        msg = _make_message(content="@pynchy what is 2+2?")
        await _seed_message(app, msg)

        fake_proc = FakeProcess(
            output={
                "status": "success",
                "result": "The answer is 4",
                "new_session_id": "sess-1",
            }
        )
        driver = asyncio.create_task(fake_proc.schedule_output())

        def fake_create(*args: Any, **kwargs: Any) -> Awaitable[FakeProcess]:
            return _completed_awaitable(fake_proc)

        channel = FakeChannel()
        app.channels = [channel]

        with (
            patch(f"{_CR_ORCH}.asyncio.create_subprocess_exec", fake_create),
            _patch_test_settings(tmp_path) as image_check,
        ):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)
            result = await app.process_group_messages("group@g.us")

        await driver
        assert result is TurnOutcome.COMPLETED
        assert app.sessions.get("test-group") == "sess-1"
        image_check.assert_called_once_with(
            project_root=tmp_path,
            image=app.agent_execution_runtime.agent_image,
        )
        # Output should have been sent via the channel
        assert len(channel.sent_messages) == 1
        assert "The answer is 4" in channel.sent_messages[0][1]

    async def test_trace_events_forwarded_to_channels(self, app: PynchyApp, tmp_path: Path):
        """Thinking and tool_use trace events should be sent to channels, not just results."""
        msg = _make_message(content="@pynchy do something complex")
        await _seed_message(app, msg)

        # Simulate a realistic agent session: thinking -> tool_use -> result
        fake_proc = FakeProcess()

        driver = asyncio.create_task(
            _schedule_outputs_via_session(
                fake_proc,
                _trace_session_outputs(),
                final_session_id="sess-trace",
            )
        )

        def fake_create(*args: Any, **kwargs: Any) -> Awaitable[FakeProcess]:
            return _completed_awaitable(fake_proc)

        channel = FakeChannel()
        app.channels = [channel]

        with (
            patch(f"{_CR_ORCH}.asyncio.create_subprocess_exec", fake_create),
            _patch_test_settings(tmp_path),
        ):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)
            result = await app.process_group_messages("group@g.us")

        await driver
        assert result is TurnOutcome.COMPLETED
        _assert_trace_order(_sent_texts(channel))

    async def test_processes_messages_without_trigger(self, app: PynchyApp):
        """Workspace config no longer gates non-admin groups on mention triggers."""
        msg = _make_message(content="just a regular message without trigger")
        await _seed_message(app, msg)
        run_agent = AsyncMock(return_value="error")
        app.run_agent = run_agent  # type: ignore[method-assign] - isolates message routing from containers.

        result = await app.process_group_messages("group@g.us")

        assert result is TurnOutcome.RETRY
        run_agent.assert_awaited_once()

    async def test_rolls_back_cursor_on_error(self, app: PynchyApp, tmp_path: Path):
        """On agent error (before any output), cursor should roll back for retry."""
        msg = _make_message(content="@pynchy fail please")
        await _seed_message(app, msg)

        fake_proc = FakeProcess()

        # Simulate error exit — _monitor_proc detects via proc.wait()
        async def schedule_error():
            await asyncio.sleep(0.05)
            fake_proc.stderr.feed_data(b"something broke\n")
            await asyncio.sleep(0.01)
            fake_proc._returncode = 1
            fake_proc.stderr.feed_eof()
            fake_proc._wait_event.set()

        driver = asyncio.create_task(schedule_error())

        def fake_create(*args: Any, **kwargs: Any) -> Awaitable[FakeProcess]:
            return _completed_awaitable(fake_proc)

        app.channels = [FakeChannel()]

        with (
            patch(f"{_CR_ORCH}.asyncio.create_subprocess_exec", fake_create),
            _patch_test_settings(tmp_path),
        ):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)
            result = await app.process_group_messages("group@g.us")

        await driver
        assert result is TurnOutcome.RETRY
        # Cursor should NOT have been advanced (rolled back)
        assert not app.last_agent_timestamp.get("group@g.us", "")

    async def test_main_group_processes_without_trigger(self, app: PynchyApp, tmp_path: Path):
        """Admin group processes all messages without requiring a trigger mention."""
        app.workspaces = {
            "main@g.us": WorkspaceProfile(
                jid="main@g.us",
                name="Main",
                folder="main",
                trigger="always",
                added_at="2024-01-01T00:00:00.000Z",
                is_admin=True,
            ),
        }
        msg = _make_message(chat_jid="main@g.us", content="no trigger needed")
        await _seed_message(app, msg)

        fake_proc = FakeProcess(
            output={
                "status": "success",
                "result": "Got it",
                "new_session_id": "s-main",
            },
            group_folder="main",
        )
        driver = asyncio.create_task(fake_proc.schedule_output())

        def fake_create(*args: Any, **kwargs: Any) -> Awaitable[FakeProcess]:
            return _completed_awaitable(fake_proc)

        app.channels = [FakeChannel()]

        worktree_path = tmp_path / "worktrees" / "main"
        worktree_path.mkdir(parents=True)
        fake_wt = MagicMock()
        fake_wt.path = worktree_path
        fake_wt.notices = []

        with (
            patch(f"{_CR_ORCH}.asyncio.create_subprocess_exec", fake_create),
            _patch_test_settings(tmp_path),
            patch("pynchy.host.git_ops.worktree.ensure_worktree", return_value=fake_wt),
        ):
            (tmp_path / "groups" / "main").mkdir(parents=True)
            result = await app.process_group_messages("main@g.us")

        await driver
        assert result is TurnOutcome.COMPLETED


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

        def fake_create(*args: Any, **kwargs: Any) -> Awaitable[FakeProcess]:
            return _completed_awaitable(fake_proc)

        group = app.workspaces["group@g.us"]

        with (
            patch(f"{_CR_ORCH}.asyncio.create_subprocess_exec", fake_create),
            _patch_test_settings(tmp_path),
        ):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)
            result = await app.queue.run_serialized_task(
                RuntimeTarget.from_workspace(group),
                "test-run-agent-success",
                lambda: app.run_agent(
                    group,
                    "group@g.us",
                    [{"message_type": "user", "content": "test prompt"}],
                ),
            )

        await driver
        assert result == "success"
        assert app.sessions.get("test-group") == "s-1"

    async def test_returns_error_on_exception(self, app: PynchyApp, tmp_path: Path):
        def failing_create(*args: Any, **kwargs: Any) -> Awaitable[None]:
            return _failed_awaitable(RuntimeError("spawn failed"))

        group = app.workspaces["group@g.us"]

        with (
            patch(f"{_CR_ORCH}.asyncio.create_subprocess_exec", failing_create),
            _patch_test_settings(tmp_path),
        ):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)
            result = await app.queue.run_serialized_task(
                RuntimeTarget.from_workspace(group),
                "test-run-agent-error",
                lambda: app.run_agent(
                    group,
                    "group@g.us",
                    [{"message_type": "user", "content": "test prompt"}],
                ),
            )

        assert result == "error"


class TestRecoverPendingMessages:
    """Test startup crash recovery."""

    async def test_enqueues_groups_with_pending_messages(self, app: PynchyApp):
        # Store a message but don't advance the cursor
        msg = _make_message(content="missed message")
        await _seed_message(app, msg)

        started = []

        def _start_turn(jid: str) -> Awaitable[None]:
            started.append(jid)
            return _completed_awaitable()

        app.start_interactive_turn = _start_turn  # type: ignore[method-assign]

        await startup_handler.recover_pending_messages(app)
        assert "group@g.us" in started

    async def test_skips_groups_with_no_pending_messages(self, app: PynchyApp):
        # No messages stored at all
        started = []

        def _start_turn(jid: str) -> Awaitable[None]:
            started.append(jid)
            return _completed_awaitable()

        app.start_interactive_turn = _start_turn  # type: ignore[method-assign]

        await startup_handler.recover_pending_messages(app)
        assert len(started) == 0


class TestStatePersistence:
    """Test state load/save round-trips."""

    async def test_save_and_load_state(self, app: PynchyApp):
        app.last_timestamp = "2024-06-01T12:00:00Z"
        app.last_agent_timestamp = {"group@g.us": "2024-06-01T11:00:00Z"}
        await app.save_state()

        # Create a new app and load state
        app2 = PynchyApp()
        await app2.load_state()
        assert app2.last_timestamp == "2024-06-01T12:00:00Z"
        assert app2.last_agent_timestamp == {"group@g.us": "2024-06-01T11:00:00Z"}

    async def test_load_state_handles_corrupted_json(self, app: PynchyApp):
        await set_router_state("last_agent_timestamp", "not valid json")

        app2 = PynchyApp()
        await app2.load_state()
        # Should reset to empty dict, not crash
        assert app2.last_agent_timestamp == {}


class TestTraceLocalPersistence:
    """Verify that SQLite history keeps messages but not Phoenix-owned traces."""

    async def test_thinking_and_tool_use_not_persisted(self, app: PynchyApp, tmp_path: Path):
        """Thinking and tool_use events should not be copied into chat history."""
        msg = _make_message(content="@pynchy do something")
        await _seed_message(app, msg)

        fake_proc = FakeProcess()

        trace_outputs = [
            {"type": "thinking", "status": "success", "thinking": "Let me think..."},
            {
                "type": "tool_use",
                "status": "success",
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
            },
            {
                "type": "result",
                "status": "success",
                "result": "Done",
                "new_session_id": "sess-trace",
            },
            {"status": "success", "result": None, "new_session_id": "sess-trace"},
        ]

        driver = asyncio.create_task(
            _schedule_outputs_via_session(fake_proc, trace_outputs, final_session_id="sess-trace")
        )

        def fake_create(*args: Any, **kwargs: Any) -> Awaitable[FakeProcess]:
            return _completed_awaitable(fake_proc)

        channel = FakeChannel()
        app.channels = [channel]

        with (
            patch(f"{_CR_ORCH}.asyncio.create_subprocess_exec", fake_create),
            _patch_test_settings(tmp_path),
        ):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)
            await app.process_group_messages("group@g.us")

        await driver

        history = await get_chat_history("group@g.us", limit=50)
        senders = {m.sender for m in history}
        assert "bot" in senders, f"Expected 'bot' in senders, got {senders}"
        assert "thinking" not in senders, f"Unexpected thinking trace in {senders}"
        assert "tool_use" not in senders, f"Unexpected tool trace in {senders}"

    async def test_system_trace_not_persisted(self, app: PynchyApp, tmp_path: Path):
        """System trace payloads should not be copied into chat history."""
        msg = _make_message(content="@pynchy hello")
        await _seed_message(app, msg)

        fake_proc = FakeProcess()

        trace_outputs = [
            {
                "type": "system",
                "status": "success",
                "system_subtype": "init",
                "system_data": {"session_id": "sess-sys"},
            },
            {"type": "result", "status": "success", "result": "Hi", "new_session_id": "sess-sys"},
            {"status": "success", "result": None, "new_session_id": "sess-sys"},
        ]

        driver = asyncio.create_task(
            _schedule_outputs_via_session(fake_proc, trace_outputs, final_session_id="sess-sys")
        )

        def fake_create(*args: Any, **kwargs: Any) -> Awaitable[FakeProcess]:
            return _completed_awaitable(fake_proc)

        channel = FakeChannel()
        app.channels = [channel]

        with (
            patch(f"{_CR_ORCH}.asyncio.create_subprocess_exec", fake_create),
            _patch_test_settings(tmp_path),
        ):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)
            await app.process_group_messages("group@g.us")

        await driver

        history = await get_chat_history("group@g.us", limit=50)
        system_msgs = [m for m in history if m.sender == "system"]
        assert system_msgs == []

    async def test_result_metadata_not_persisted(self, app: PynchyApp, tmp_path: Path):
        """Result metadata should not be copied into chat history."""
        msg = _make_message(content="@pynchy hello")
        await _seed_message(app, msg)

        fake_proc = FakeProcess()

        trace_outputs = [
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
            },
            {"status": "success", "result": None, "new_session_id": "sess-meta"},
        ]

        driver = asyncio.create_task(
            _schedule_outputs_via_session(fake_proc, trace_outputs, final_session_id="sess-meta")
        )

        def fake_create(*args: Any, **kwargs: Any) -> Awaitable[FakeProcess]:
            return _completed_awaitable(fake_proc)

        channel = FakeChannel()
        app.channels = [channel]

        with (
            patch(f"{_CR_ORCH}.asyncio.create_subprocess_exec", fake_create),
            _patch_test_settings(tmp_path),
        ):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)
            await app.process_group_messages("group@g.us")

        await driver

        history = await get_chat_history("group@g.us", limit=50)
        meta_msgs = [m for m in history if m.sender == "result_meta"]
        assert meta_msgs == []

        # Channel should have received the formatted cost message
        texts = [text for _, text in channel.sent_messages]
        assert any("0.03 USD" in t for t in texts), f"Expected cost in channel, got {texts}"


class TestDeployContinuationResume:
    """Verify durable multi-group work resumption after a restart."""

    async def test_resumes_all_durable_in_flight_turns(self, app: PynchyApp, tmp_path: Path):
        """Startup dispatches each recorded running turn, including scheduled work."""
        await state.init_test_database()

        # Register two groups
        app.workspaces = {
            "admin-1@g.us": WorkspaceProfile(
                jid="admin-1@g.us",
                name="Admin",
                folder="admin-1",
                trigger="always",
                added_at="2024-01-01T00:00:00.000Z",
                is_admin=True,
            ),
            "team@g.us": WorkspaceProfile(
                jid="team@g.us",
                name="Team",
                folder="team",
                trigger="@pynchy",
                added_at="2024-01-01T00:00:00.000Z",
            ),
        }

        for turn in (
            InFlightTurn(
                turn_id="turn-admin",
                chat_jid="admin-1@g.us",
                group_folder="admin-1",
                work_kind=InFlightWorkKind.INTERACTIVE,
                input_messages=[{"content": "finish admin work"}],
                input_start_cursor="old-admin",
                input_end_cursor="new-admin",
                started_at="2026-07-14T10:00:00+00:00",
            ),
            InFlightTurn(
                turn_id="turn-team",
                chat_jid="team@g.us",
                group_folder="team",
                work_kind=InFlightWorkKind.SCHEDULED,
                input_messages=[{"content": "finish team job"}],
                input_start_cursor="",
                input_end_cursor="",
                started_at="2026-07-14T10:00:01+00:00",
                task_id="task-team",
            ),
        ):
            await state.begin_in_flight_turn(turn)

        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        continuation = {
            "chat_jid": "admin-1@g.us",
            "resume_prompt": "Deploy complete.",
            "commit_sha": "abc12345",
            "previous_commit_sha": "000",
            "interrupted_turns": ["turn-admin", "turn-team"],
        }
        (data_dir / "deploy_continuation.json").write_text(json.dumps(continuation))

        started: list[tuple[str, str]] = []

        def _start_turn(turn_id: str, group_folder: str) -> Awaitable[None]:
            started.append((turn_id, group_folder))
            return _completed_awaitable()

        app.start_interrupted_turn = _start_turn  # type: ignore[method-assign]

        with (
            patch("pynchy.host.orchestrator.startup_handler.get_settings") as mock_settings,
            patch(
                "pynchy.host.orchestrator.startup_handler.get_head_commit_message",
                return_value="test commit",
            ),
        ):
            s = MagicMock()
            s.data_dir = data_dir
            mock_settings.return_value = s

            await check_deploy_continuation(
                app,
                active_revision=DeployRevision("abc12345", "active-config"),
            )

        assert set(started) == {
            ("turn-admin", "admin-1"),
            ("turn-team", "team"),
        }

        # Both groups should have a deploy resume message in history
        admin_history = await get_chat_history("admin-1@g.us", limit=10)
        team_history = await get_chat_history("team@g.us", limit=10)
        assert any("Deploy complete" in m.content for m in admin_history)
        assert any("Deploy complete" in m.content for m in team_history)

        # Continuation file should be deleted
        assert not (data_dir / "deploy_continuation.json").exists()

    async def test_skips_when_no_durable_in_flight_turns(self, app: PynchyApp, tmp_path: Path):
        """Idle session metadata in a legacy deploy file does not trigger work."""
        await state.init_test_database()

        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        continuation = {
            "chat_jid": "admin-1@g.us",
            "resume_prompt": "Deploy complete.",
            "commit_sha": "abc12345",
            "previous_commit_sha": "000",
            "active_sessions": {"admin-1@g.us": "idle-session"},
        }
        (data_dir / "deploy_continuation.json").write_text(json.dumps(continuation))

        started: list[str] = []

        def _start_turn(turn_id: str, chat_jid: str) -> Awaitable[None]:
            started.append(turn_id)
            return _completed_awaitable()

        app.start_interrupted_turn = _start_turn  # type: ignore[method-assign]

        with patch("pynchy.host.orchestrator.startup_handler.get_settings") as mock_settings:
            s = MagicMock()
            s.data_dir = data_dir
            mock_settings.return_value = s

            await check_deploy_continuation(
                app,
                active_revision=DeployRevision("abc12345", "active-config"),
            )

        assert len(started) == 0
