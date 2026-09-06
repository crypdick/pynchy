"""Tests that scheduled work uses the thread's durable provider session.

These tests verify the session-based orchestration in the public agent entry point
(owned session and IPC watcher stream events in real-time), not the
end-to-end output routing (which is tested via the IPC watcher tests).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import (
    make_container_agent_operations,
    make_container_runtime_operations,
    make_host_runtime_operations,
)

from pynchy.agent_protocol.api import (
    AgentExecutionRuntime,
    ContainerInput,
)
from pynchy.host.container_manager.session import ContainerSession, SessionDiedError
from pynchy.host.orchestrator import agent_runner
from pynchy.host.orchestrator.concurrency import GroupQueue
from pynchy.identifiers import RuntimeId
from pynchy.workspace.api import (
    RuntimeTarget,
    WorkspaceProfile,
)

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
        self.session_cleared: set[str] = set()
        self.workspaces: dict[str, WorkspaceProfile] = {}
        self.queue = GroupQueue(
            10,
            make_container_runtime_operations(),
        )
        self.plugin_manager = None
        self.container_agent_operations = make_container_agent_operations()
        self.host_runtime_operations = make_host_runtime_operations()
        self.agent_execution_runtime = AgentExecutionRuntime(
            project_root=Path("test-project"),
            groups_dir=Path("test-project/groups"),
            data_dir=Path("test-project/data"),
            mount_allowlist_path=Path("test-project/mount-allowlist.toml"),
            blocked_mount_patterns=(),
            agent_image="pynchy-agent:latest",
            agent_memory_mb=2048,
            container_timeout=300.0,
            default_core="openai",
            idle_timeout=60.0,
            model="",
            model_reasoning_effort=None,
        )
        self._broadcast_calls: list = []

    async def get_available_groups(self) -> list[dict[str, Any]]:
        return []

    async def broadcast_agent_input(
        self, chat_jid: str, messages: list[dict], *, source: str = "user"
    ) -> None:
        self._broadcast_calls.append((chat_jid, messages, source))

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None:
        return None

    def refresh_personalized_agent_skills(self, group_folder: str) -> None:
        del group_folder

    def admin_repo_notices(
        self, group_folder: str, *, is_admin: bool, repo_access: str | None
    ) -> list[str]:
        del group_folder, is_admin, repo_access
        return []


def _make_pre_container_result():
    """Build a fake PreContainerResult with all required fields."""
    return agent_runner.PreContainerResult(
        is_admin=False,
        repo_access=None,
        repo_accesses=[],
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


def _make_container_input() -> MagicMock:
    """Create a mock ContainerInput with a real invocation_ts float."""
    input_data = MagicMock(spec=ContainerInput)
    input_data.invocation_ts = 0.0
    input_data.turn_id = "turn-test"
    input_data.query_id = "query-test"
    return input_data


# Patch targets — at the call site (pynchy.host.orchestrator.agent_runner).
_P_BUILD = "pynchy.host.orchestrator.agent_runner.build_container_input"
_P_CLEAR_SESSION = "pynchy.host.orchestrator.agent_runner.clear_session"
_P_PREFLIGHT = "pynchy.host.orchestrator.agent_runner.pre_container_setup"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestScheduledTaskUsesSession:
    """Verify scheduled tasks use the session-based execution path."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.group = _make_group()
        self.deps = _FakeDeps()
        self.ctx = _make_pre_container_result()
        self.fake_proc = _make_fake_proc()
        self.fake_session = _make_fake_session()
        self.fake_session.proc = self.fake_proc
        self.fake_session.container_name = "c-123"
        self.start_session = AsyncMock(return_value=(self.fake_session, ()))
        self.destroy_session = AsyncMock()

        async def wait_for_query(session: ContainerSession, query_timeout_seconds: float) -> bool:
            try:
                await session.wait_for_query_done(query_timeout_seconds=query_timeout_seconds)
            except SessionDiedError:
                return False
            return True

        self.deps.container_agent_operations = replace(
            self.deps.container_agent_operations,
            start_session=self.start_session,
            destroy_session=self.destroy_session,
            wait_for_query=wait_for_query,
        )

    async def _call(self):
        """Drive the public agent entry point through its scheduled-task path."""
        with patch(_P_PREFLIGHT, new_callable=AsyncMock, return_value=self.ctx):
            return await self.deps.queue.run_serialized_task(
                RuntimeTarget.from_workspace(self.group),
                "test-scheduled-task",
                lambda: agent_runner.run_agent(
                    self.deps,
                    self.group,
                    "test@g.us",
                    [{"content": "do stuff", "sender": "task"}],
                    is_scheduled_task=True,
                    input_source="scheduled_task",
                ),
            )

    @pytest.mark.asyncio
    async def test_creates_session_with_configured_idle_timeout(self):
        """One-shot tasks use the same idle termination policy as other containers."""
        with (
            patch(_P_BUILD, return_value=_make_container_input()),
            patch(_P_CLEAR_SESSION, new_callable=AsyncMock),
        ):
            await self._call()

        self.start_session.assert_awaited_once()
        runtime = self.start_session.call_args.args[2]
        assert runtime.idle_timeout > 0
        assert runtime.data_dir == self.deps.agent_execution_runtime.data_dir

    @pytest.mark.asyncio
    async def test_resumes_a_persisted_provider_session_when_spawning_worker(self):
        """A disposable worker receives the thread's durable provider identity."""
        self.ctx.session_id = "durable-provider-session"
        build_input = MagicMock(return_value=_make_container_input())

        with (
            patch(_P_BUILD, build_input),
            patch(_P_CLEAR_SESSION, new_callable=AsyncMock),
        ):
            await self._call()

        assert build_input.call_args.args[1].session_id == "durable-provider-session"

    @pytest.mark.asyncio
    async def test_sets_output_handler_on_session(self):
        """Session should have the wrapped_on_output handler set, enabling
        real-time streaming through the IPC watcher."""
        with (
            patch(_P_BUILD, return_value=_make_container_input()),
            patch(_P_CLEAR_SESSION, new_callable=AsyncMock),
        ):
            await self._call()

        self.fake_session.set_output_handler.assert_called_once_with(
            self.ctx.wrapped_on_output,
            query_id="query-test",
        )

    @pytest.mark.asyncio
    async def test_waits_for_query_done_with_config_timeout(self):
        """Should wait for session query completion, not process exit."""
        with (
            patch(_P_BUILD, return_value=_make_container_input()),
            patch(_P_CLEAR_SESSION, new_callable=AsyncMock),
        ):
            await self._call()

        self.fake_session.wait_for_query_done.assert_awaited_once_with(query_timeout_seconds=300.0)

    @pytest.mark.asyncio
    async def test_scheduled_progress_extends_configured_silence_timeout(self):
        """Scheduled turns inherit the shared progress-aware session policy."""
        self.ctx.config_timeout = 0.1
        input_data = _make_container_input()
        session = ContainerSession("test-group", "pynchy-test-group-progress")
        session.proc = self.fake_proc
        driver_tasks: list[asyncio.Task[None]] = []

        async def create_progressing_session(*_args: object, **_kwargs: object):
            async def drive_query() -> None:
                await asyncio.sleep(0.06)
                session.signal_query_progress("query-test")
                await asyncio.sleep(0.06)
                session.signal_query_done("query-test")

            driver_tasks.append(asyncio.create_task(drive_query()))
            await asyncio.sleep(0)
            return session, ()

        self.start_session.side_effect = create_progressing_session
        with (
            patch(_P_BUILD, return_value=input_data),
            patch(_P_CLEAR_SESSION, new_callable=AsyncMock),
        ):
            result = await self._call()

        await asyncio.gather(*driver_tasks)
        assert result == "success"
        self.destroy_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_timeout_destroys_session_and_returns_error(self):
        """On timeout, should destroy the session and return 'error'."""
        self.fake_session.wait_for_query_done.side_effect = TimeoutError()

        with (
            patch(_P_BUILD, return_value=_make_container_input()),
            patch(_P_CLEAR_SESSION, new_callable=AsyncMock),
        ):
            result = await self._call()

        assert result == "error"
        self.destroy_session.assert_awaited_once_with("test-group")

    @pytest.mark.asyncio
    async def test_session_died_returns_error(self):
        """On SessionDiedError, should return 'error'."""
        self.fake_session.wait_for_query_done.side_effect = SessionDiedError("container died")

        with (
            patch(_P_BUILD, return_value=_make_container_input()),
            patch(_P_CLEAR_SESSION, new_callable=AsyncMock),
        ):
            result = await self._call()

        assert result == "error"

    @pytest.mark.asyncio
    async def test_completion_preserves_durable_session_reference(self):
        """Worker completion must not discard the thread's provider identity."""
        self.deps.sessions["test-group"] = "some-session-id"

        with (
            patch(_P_BUILD, return_value=_make_container_input()),
            patch(_P_CLEAR_SESSION, new_callable=AsyncMock),
        ):
            await self._call()

        assert self.deps.sessions["test-group"] == "some-session-id"
        self.destroy_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_registers_process_on_queue(self):
        """Should register the container process for send_message() support."""
        registered = []

        def track_register(runtime_id, proc, name, invocation_ts=0.0):
            registered.append((runtime_id, proc, name, invocation_ts))

        self.deps.queue.register_process = track_register

        with (
            patch(_P_BUILD, return_value=_make_container_input()),
            patch(_P_CLEAR_SESSION, new_callable=AsyncMock),
        ):
            await self._call()

        assert len(registered) == 1
        assert registered[0] == (RuntimeId("test-group"), self.fake_proc, "c-123", 0.0)

    @pytest.mark.asyncio
    async def test_spawn_failure_returns_error(self):
        """If session startup raises OSError, return 'error' gracefully."""
        self.start_session.side_effect = OSError("docker not found")
        with (
            patch(_P_BUILD, return_value=_make_container_input()),
            patch(_P_CLEAR_SESSION, new_callable=AsyncMock),
        ):
            result = await self._call()

        assert result == "error"

    # ------------------------------------------------------------------
    # Phase 2: Deploy resume — CancelledError preserves session
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_cancelled_error_does_not_destroy_session(self):
        """On CancelledError (deploy SIGTERM), session should NOT be destroyed
        so deploy continuation can resume the task on restart."""
        self.fake_session.wait_for_query_done.side_effect = asyncio.CancelledError()

        with (
            patch(_P_BUILD, return_value=_make_container_input()),
            pytest.raises(asyncio.CancelledError),
        ):
            await self._call()

        self.destroy_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancelled_error_preserves_deps_sessions(self):
        """On CancelledError, deps.sessions should NOT be popped so the
        durable in-flight turn can resume the same agent thread."""
        self.deps.sessions["test-group"] = "some-session-id"
        self.fake_session.wait_for_query_done.side_effect = asyncio.CancelledError()

        with (
            patch(_P_BUILD, return_value=_make_container_input()),
            pytest.raises(asyncio.CancelledError),
        ):
            await self._call()

        assert "test-group" in self.deps.sessions

    @pytest.mark.asyncio
    async def test_normal_completion_preserves_db_session(self):
        """Normal completion leaves the provider session resumable."""
        with (
            patch(_P_BUILD, return_value=_make_container_input()),
            patch(_P_CLEAR_SESSION, new_callable=AsyncMock) as mock_clear,
        ):
            await self._call()

        mock_clear.assert_not_awaited()
