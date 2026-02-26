"""Tests that _run_scheduled_task uses session-based real-time streaming.

The fix converts one-shot containers from batch output collection
(run_container_agent → read files post-exit) to the session-based pattern
(create_session → IPC watcher streams events in real-time).

These tests verify the new session-based orchestration in _run_scheduled_task,
NOT the end-to-end output routing (which is tested via the IPC watcher tests).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.container_runner._session import ContainerSession, SessionDiedError
from pynchy.group_queue import GroupQueue
from pynchy.types import ContainerInput, WorkspaceProfile

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_group(folder: str = "test-group") -> WorkspaceProfile:
    return WorkspaceProfile(
        jid="test@g.us",
        name="Test Group",
        folder=folder,
        trigger="@bot",
        added_at=datetime.now(UTC).isoformat(),
    )


class _FakeDeps:
    """Minimal mock satisfying the AgentRunnerDeps protocol."""

    def __init__(self):
        self.sessions: dict[str, str] = {}
        self._session_cleared: set[str] = set()
        self.workspaces: dict[str, WorkspaceProfile] = {}
        self.queue = GroupQueue()
        self.plugin_manager = None
        self._broadcast_calls: list = []

    async def get_available_groups(self) -> list[dict[str, Any]]:
        return []

    async def broadcast_agent_input(
        self, chat_jid: str, messages: list[dict], *, source: str = "user"
    ) -> None:
        self._broadcast_calls.append((chat_jid, messages, source))


def _make_pre_container_result():
    """Build a fake _PreContainerResult with all required fields."""
    from pynchy.agent_runner import _PreContainerResult

    return _PreContainerResult(
        is_admin=False,
        repo_access=None,
        system_prompt_append=None,
        session_id=None,
        system_notices=[],
        agent_core_module="agent_runner.cores.claude",
        agent_core_class="ClaudeAgentCore",
        wrapped_on_output=AsyncMock(),
        config_timeout=300.0,
        snapshot_ms=1.0,
    )


def _make_fake_proc() -> MagicMock:
    """Create a fake asyncio.subprocess.Process."""
    proc = MagicMock(spec=asyncio.subprocess.Process)
    proc.returncode = None
    proc.stderr = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    return proc


def _make_fake_session() -> MagicMock:
    """Create a mock ContainerSession."""
    session = MagicMock(spec=ContainerSession)
    session.set_output_handler = MagicMock()
    session.wait_for_query_done = AsyncMock()
    session.proc = _make_fake_proc()
    session.container_name = "pynchy-test-group-123"
    session.is_alive = True
    return session


# Patch targets — at the call site (pynchy.agent_runner).
_P_SETUP = "pynchy.agent_runner._pre_container_setup"
_P_BUILD = "pynchy.agent_runner._build_container_input"
_P_SPAWN = "pynchy.agent_runner._spawn_container"
_P_CREATE = "pynchy.agent_runner.create_session"
_P_DESTROY = "pynchy.agent_runner.destroy_session"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestScheduledTaskUsesSession:
    """Verify _run_scheduled_task uses the session-based pattern."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.group = _make_group()
        self.deps = _FakeDeps()
        self.ctx = _make_pre_container_result()
        self.fake_proc = _make_fake_proc()
        self.fake_session = _make_fake_session()

    async def _call(self):
        """Call _run_scheduled_task with standard mocks."""
        from pynchy.agent_runner import _run_scheduled_task

        return await _run_scheduled_task(
            self.deps,
            self.group,
            "test@g.us",
            [{"content": "do stuff", "sender": "task"}],
            None,  # on_output
            None,  # extra_system_notices
            None,  # repo_access_override
            "scheduled_task",
        )

    @pytest.mark.asyncio
    async def test_creates_session_with_zero_idle_timeout(self):
        """One-shot tasks should create session with idle_timeout_override=0.0
        so the container isn't killed by idle timeout during a long run."""
        with (
            patch(_P_SETUP, new_callable=AsyncMock, return_value=self.ctx),
            patch(_P_BUILD, return_value=MagicMock(spec=ContainerInput)),
            patch(_P_SPAWN, new_callable=AsyncMock, return_value=(self.fake_proc, "c-123", [])),
            patch(_P_CREATE, new_callable=AsyncMock, return_value=self.fake_session) as mock_cs,
            patch(_P_DESTROY, new_callable=AsyncMock),
        ):
            await self._call()

        mock_cs.assert_awaited_once()
        _, kwargs = mock_cs.call_args
        assert kwargs.get("idle_timeout_override") == 0.0, (
            "create_session must be called with idle_timeout_override=0.0"
        )

    @pytest.mark.asyncio
    async def test_sets_output_handler_on_session(self):
        """Session should have the wrapped_on_output handler set, enabling
        real-time streaming through the IPC watcher."""
        with (
            patch(_P_SETUP, new_callable=AsyncMock, return_value=self.ctx),
            patch(_P_BUILD, return_value=MagicMock(spec=ContainerInput)),
            patch(_P_SPAWN, new_callable=AsyncMock, return_value=(self.fake_proc, "c-123", [])),
            patch(_P_CREATE, new_callable=AsyncMock, return_value=self.fake_session),
            patch(_P_DESTROY, new_callable=AsyncMock),
        ):
            await self._call()

        self.fake_session.set_output_handler.assert_called_once_with(self.ctx.wrapped_on_output)

    @pytest.mark.asyncio
    async def test_waits_for_query_done_with_config_timeout(self):
        """Should wait for session query completion, not process exit."""
        with (
            patch(_P_SETUP, new_callable=AsyncMock, return_value=self.ctx),
            patch(_P_BUILD, return_value=MagicMock(spec=ContainerInput)),
            patch(_P_SPAWN, new_callable=AsyncMock, return_value=(self.fake_proc, "c-123", [])),
            patch(_P_CREATE, new_callable=AsyncMock, return_value=self.fake_session),
            patch(_P_DESTROY, new_callable=AsyncMock),
        ):
            await self._call()

        self.fake_session.wait_for_query_done.assert_awaited_once_with(timeout=300.0)

    def test_run_container_agent_not_imported(self):
        """run_container_agent should no longer be imported in agent_runner."""
        import pynchy.agent_runner as mod

        assert not hasattr(mod, "run_container_agent"), (
            "run_container_agent should not be imported — "
            "scheduled tasks now use session-based streaming"
        )

    @pytest.mark.asyncio
    async def test_timeout_destroys_session_and_returns_error(self):
        """On timeout, should destroy the session and return 'error'."""
        self.fake_session.wait_for_query_done.side_effect = TimeoutError()

        with (
            patch(_P_SETUP, new_callable=AsyncMock, return_value=self.ctx),
            patch(_P_BUILD, return_value=MagicMock(spec=ContainerInput)),
            patch(_P_SPAWN, new_callable=AsyncMock, return_value=(self.fake_proc, "c-123", [])),
            patch(_P_CREATE, new_callable=AsyncMock, return_value=self.fake_session),
            patch(_P_DESTROY, new_callable=AsyncMock) as mock_destroy,
        ):
            result = await self._call()

        assert result == "error"
        # destroy_session: once at top (clean slate), once for timeout, once in finally
        assert mock_destroy.await_count >= 2, (
            "destroy_session should be called for timeout cleanup and in finally"
        )

    @pytest.mark.asyncio
    async def test_session_died_returns_error(self):
        """On SessionDiedError, should return 'error'."""
        self.fake_session.wait_for_query_done.side_effect = SessionDiedError("container died")

        with (
            patch(_P_SETUP, new_callable=AsyncMock, return_value=self.ctx),
            patch(_P_BUILD, return_value=MagicMock(spec=ContainerInput)),
            patch(_P_SPAWN, new_callable=AsyncMock, return_value=(self.fake_proc, "c-123", [])),
            patch(_P_CREATE, new_callable=AsyncMock, return_value=self.fake_session),
            patch(_P_DESTROY, new_callable=AsyncMock),
        ):
            result = await self._call()

        assert result == "error"

    @pytest.mark.asyncio
    async def test_finally_cleans_up_session_and_deps(self):
        """Should always clean up session and deps.sessions in finally block."""
        self.deps.sessions["test-group"] = "some-session-id"

        with (
            patch(_P_SETUP, new_callable=AsyncMock, return_value=self.ctx),
            patch(_P_BUILD, return_value=MagicMock(spec=ContainerInput)),
            patch(_P_SPAWN, new_callable=AsyncMock, return_value=(self.fake_proc, "c-123", [])),
            patch(_P_CREATE, new_callable=AsyncMock, return_value=self.fake_session),
            patch(_P_DESTROY, new_callable=AsyncMock) as mock_destroy,
        ):
            await self._call()

        # deps.sessions should have been popped
        assert "test-group" not in self.deps.sessions
        # destroy_session called in finally
        mock_destroy.assert_awaited()

    @pytest.mark.asyncio
    async def test_registers_process_on_queue(self):
        """Should register the container process for send_message() support."""
        registered = []

        def track_register(chat_jid, proc, name, folder):
            registered.append((chat_jid, proc, name, folder))

        self.deps.queue.register_process = track_register

        with (
            patch(_P_SETUP, new_callable=AsyncMock, return_value=self.ctx),
            patch(_P_BUILD, return_value=MagicMock(spec=ContainerInput)),
            patch(_P_SPAWN, new_callable=AsyncMock, return_value=(self.fake_proc, "c-123", [])),
            patch(_P_CREATE, new_callable=AsyncMock, return_value=self.fake_session),
            patch(_P_DESTROY, new_callable=AsyncMock),
        ):
            await self._call()

        assert len(registered) == 1
        assert registered[0] == ("test@g.us", self.fake_proc, "c-123", "test-group")

    @pytest.mark.asyncio
    async def test_spawn_failure_returns_error(self):
        """If _spawn_container raises OSError, should return 'error' gracefully."""
        with (
            patch(_P_SETUP, new_callable=AsyncMock, return_value=self.ctx),
            patch(_P_BUILD, return_value=MagicMock(spec=ContainerInput)),
            patch(_P_SPAWN, new_callable=AsyncMock, side_effect=OSError("docker not found")),
            patch(_P_DESTROY, new_callable=AsyncMock),
        ):
            result = await self._call()

        assert result == "error"
