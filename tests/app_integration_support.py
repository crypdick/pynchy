"""Integration tests for PynchyApp.

End-to-end tests that wire up real subsystems (DB, queue, message processing)
with mocked boundaries (WhatsApp channel, container subprocess, Apple Container CLI).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from conftest import NullChannel, configure_skill_activation_for, make_settings

from pynchy import state
from pynchy.agent_protocol.api import (
    AgentExecutionRuntime,
    parse_container_output,
)
from pynchy.host.container_manager.process import is_query_done_pulse
from pynchy.host.container_manager.session import destroy_all_sessions, get_session
from pynchy.host.orchestrator.app import PynchyApp
from pynchy.plugins.api import NewMessage
from pynchy.state import store_message
from pynchy.workspace.api import (
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
    configure_skill_activation_for(s)
    with contextlib.ExitStack() as stack:
        # Patch docker_rm_force which spawns a real subprocess to remove
        # containers — would hang in the test environment. Patch each import
        # site because Python's from-import creates a separate reference.
        stack.enter_context(
            patch("pynchy.host.container_manager.process.docker_rm_force", _noop_docker_rm)
        )
        stack.enter_context(
            patch("pynchy.host.container_manager.session.docker_rm_force", _noop_docker_rm)
        )
        stack.enter_context(patch("pynchy.host.orchestrator.app.docker_rm_force", _noop_docker_rm))
        yield stack.enter_context(patch(f"{_CR_ORCH}._ensure_agent_image"))


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
            output = parse_container_output(json.dumps(self._output))
            await handler(output)

            # Emit query-done pulse via signal_query_done
            pulse_data = {
                "status": "success",
                "result": None,
                "new_session_id": self._output.get("new_session_id", "test-session"),
            }
            pulse = parse_container_output(json.dumps(pulse_data))
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
        parsed = parse_container_output(json.dumps(output_dict))
        await handler(parsed)
        if is_query_done_pulse(parsed):
            emitted_pulse = True
            session.signal_query_done()

    # If no output triggered query done, append a pulse
    if not emitted_pulse:
        pulse = parse_container_output(
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
