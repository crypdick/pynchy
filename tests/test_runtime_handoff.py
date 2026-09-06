"""Regression tests for terminal control racing runtime process handoff."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import (
    make_container_agent_operations,
    make_container_runtime_operations,
    make_host_runtime_operations,
    make_settings,
)

from pynchy.agent_protocol.api import (
    AgentExecutionRuntime,
    ContainerInput,
)
from pynchy.host.container_manager.session import ContainerSession
from pynchy.host.orchestrator import host_runner
from pynchy.host.orchestrator.agent_runner import PreContainerResult, run_agent
from pynchy.host.orchestrator.concurrency import GroupQueue
from pynchy.workspace.api import (
    RuntimeTarget,
    WorkspaceProfile,
)

_TEST_GROUP = WorkspaceProfile(
    jid="test@g.us",
    name="Test Group",
    folder="test-group",
    trigger="@pynchy",
)


class _RunnerDeps:
    """Minimal public runner dependencies for handoff behavior tests."""

    def __init__(self) -> None:
        self.sessions: dict[str, str] = {}
        self.session_cleared: set[str] = set()
        self.workspaces: dict[str, WorkspaceProfile] = {}
        self.queue = MagicMock(spec=GroupQueue)
        self.plugin_manager = None
        self.agent_execution_runtime = _agent_runtime(make_settings())
        self.container_agent_operations = make_container_agent_operations()
        self.host_runtime_operations = make_host_runtime_operations()
        self.refresh_personalized_agent_skills = MagicMock()
        self.admin_repo_notices = MagicMock(return_value=[])

    async def get_available_groups(self) -> list[dict[str, Any]]:
        return []

    async def broadcast_agent_input(
        self,
        _chat_jid: str,
        _messages: list[dict[str, Any]],
        *,
        source: str = "user",
    ) -> None:
        del source

    async def broadcast_host_message(self, _chat_jid: str, _text: str) -> None:
        return None


def _agent_runtime(settings: object) -> AgentExecutionRuntime:
    return AgentExecutionRuntime(
        project_root=settings.project_root,
        groups_dir=settings.groups_dir,
        data_dir=settings.data_dir,
        mount_allowlist_path=settings.mount_allowlist_path,
        blocked_mount_patterns=tuple(settings.security.blocked_patterns),
        agent_image=settings.container.image,
        agent_memory_mb=settings.container.memory_mb,
        container_timeout=settings.container_timeout,
        default_core=settings.agent.default_core,
        idle_timeout=settings.idle_timeout,
        model=settings.agent.model,
        model_reasoning_effort=settings.agent.model_reasoning_effort,
    )


def _runner_context() -> PreContainerResult:
    return PreContainerResult(
        is_admin=False,
        repo_access=None,
        repo_accesses=[],
        system_prompt_append=None,
        session_id=None,
        system_notices=[],
        agent_core_module="agent_runner.cores.codex",
        agent_core_class="CodexCLIAgentCore",
        wrapped_on_output=AsyncMock(),
        config_timeout=30.0,
        snapshot_ms=0.0,
    )


class _HostStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _EmptyStdout:
    def __aiter__(self) -> _EmptyStdout:
        return self

    async def __anext__(self) -> bytes:
        raise StopAsyncIteration


class _HostStderr:
    async def read(self) -> bytes:
        return b""


class _RejectedHostProcess:
    def __init__(self) -> None:
        self.stdin = _HostStdin()
        self.stdout = _EmptyStdout()
        self.stderr = _HostStderr()
        self.returncode: int | None = None
        self.pid = 123

    async def wait(self) -> int:
        self.returncode = 0
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


@pytest.mark.asyncio
async def test_terminal_boundary_rejects_late_container_process_registration() -> None:
    """A process spawned after terminal retirement cannot become active."""
    queue = GroupQueue(
        1,
        make_container_runtime_operations(),
    )
    target = RuntimeTarget.from_binding("discord:terminal", "discord:terminal")
    lease = queue.acquire_host_process(target)

    await queue.stop_active_process_for_control(target.id)

    assert queue.register_process(target.id, None, "late-container") is False
    assert queue.release_host_process(lease) is False


@pytest.mark.asyncio
async def test_cold_handoff_destroys_session_when_terminal_boundary_rejects_process() -> None:
    """A cold container that loses terminal race is torn down before it can run."""
    deps = _RunnerDeps()
    deps.queue.register_process.return_value = False
    context = _runner_context()
    input_data = ContainerInput(
        messages=[],
        group_folder=_TEST_GROUP.folder,
        chat_jid=_TEST_GROUP.jid,
        is_admin=False,
    )
    session = MagicMock()
    proc = MagicMock(spec=asyncio.subprocess.Process)

    with (
        patch(
            "pynchy.host.orchestrator.agent_runner.pre_container_setup",
            new=AsyncMock(return_value=context),
        ),
        patch("pynchy.host.orchestrator.agent_runner._host_execution_cwd", return_value=None),
        patch.object(deps.container_agent_operations, "get_session", return_value=None),
        patch(
            "pynchy.host.orchestrator.agent_runner.build_container_input",
            return_value=input_data,
        ),
        patch.object(
            deps.container_agent_operations,
            "fresh_container_name",
            new=AsyncMock(return_value="cold"),
        ),
        patch.object(
            deps.container_agent_operations,
            "spawn",
            new=AsyncMock(return_value=(proc, "cold", [], ())),
        ),
        patch.object(
            deps.container_agent_operations,
            "create_session",
            new=AsyncMock(return_value=session),
        ),
        patch.object(
            deps.container_agent_operations,
            "destroy_session",
            new_callable=AsyncMock,
        ) as destroy_session,
        patch(
            "pynchy.host.orchestrator.agent_runner._await_query",
            new=AsyncMock(return_value="success"),
        ) as await_query,
    ):
        result = await run_agent(deps, _TEST_GROUP, _TEST_GROUP.jid, [{"content": "stale"}])

    assert result == "interrupted"
    destroy_session.assert_awaited_once_with(_TEST_GROUP.folder)
    session.set_output_handler.assert_not_called()
    await_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_warm_handoff_suppresses_ipc_when_terminal_boundary_rejects_process() -> None:
    """A live session cannot receive input after terminal retirement wins."""
    deps = _RunnerDeps()
    deps.queue.register_process.return_value = False
    context = _runner_context()
    session = ContainerSession(_TEST_GROUP.folder, "warm")
    session.proc = MagicMock(spec=asyncio.subprocess.Process)
    session.proc.returncode = None
    session.container_name = "warm"
    session.send_ipc_message = AsyncMock()

    with (
        patch(
            "pynchy.host.orchestrator.agent_runner.pre_container_setup",
            new=AsyncMock(return_value=context),
        ),
        patch("pynchy.host.orchestrator.agent_runner._host_execution_cwd", return_value=None),
        patch.object(deps.container_agent_operations, "get_session", return_value=session),
        patch.object(
            deps.container_agent_operations,
            "ensure_workspace_mcp",
            new=AsyncMock(return_value=()),
        ),
        patch.object(deps, "refresh_personalized_agent_skills"),
        patch(
            "pynchy.host.orchestrator.agent_runner._await_query",
            new=AsyncMock(return_value="success"),
        ) as await_query,
        patch.object(
            session,
            "set_output_handler",
            wraps=session.set_output_handler,
        ) as set_output_handler,
    ):
        result = await run_agent(deps, _TEST_GROUP, _TEST_GROUP.jid, [{"content": "stale"}])

    assert result == "interrupted"
    set_output_handler.assert_not_called()
    session.send_ipc_message.assert_not_awaited()
    await_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_host_runner_stops_process_when_handoff_registration_is_rejected(tmp_path) -> None:
    """A direct host worker stops before receiving a payload when registration loses."""
    proc = _RejectedHostProcess()
    input_data = ContainerInput(
        messages=[],
        group_folder="host-terminal",
        chat_jid="discord:terminal",
        is_admin=True,
    )

    with (
        patch(
            "pynchy.host.orchestrator.host_runner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ),
        patch(
            "pynchy.host.orchestrator.host_runner.stop_host_process",
            new_callable=AsyncMock,
        ) as stop_host_process,
    ):
        result = await host_runner.run_host_input(
            input_data,
            cwd=tmp_path,
            project_root=tmp_path,
            on_output=AsyncMock(),
            timeout_seconds=30,
            on_process_started=lambda _proc: False,
        )

    assert result == "interrupted"
    stop_host_process.assert_awaited_once_with(proc)
    assert proc.stdin.writes == []
