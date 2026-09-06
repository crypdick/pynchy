"""Public edge behavior for direct host execution helpers."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from pynchy.agent_protocol.api import ContainerInput
from pynchy.host.orchestrator import host_execution
from pynchy.host.orchestrator.host_execution import (
    HostAgentTurnRequest,
    HostExecutionCwd,
    HostRuntimeOperations,
)
from pynchy.host.orchestrator.queue_state import HostProcessLease
from pynchy.identifiers import RuntimeId
from pynchy.workspace.api import RuntimeTarget

if TYPE_CHECKING:
    from pathlib import Path


class _Queue:
    def __init__(self, *, pending: bool = False) -> None:
        self.lease = HostProcessLease(RuntimeId("group"), 1, True)
        self.pending = pending
        self.released: list[HostProcessLease] = []

    def acquire_host_process(self, _target: RuntimeTarget) -> HostProcessLease:
        return self.lease

    def register_host_process(self, *_args: object, **_kwargs: object) -> bool:
        return True

    def boundary_interrupt_requested(self, _runtime_id: RuntimeId) -> bool:
        return False

    def release_host_process(self, lease: HostProcessLease) -> bool:
        self.released.append(lease)
        return self.pending


def _request(tmp_path: Path, queue: _Queue) -> HostAgentTurnRequest:
    return HostAgentTurnRequest(
        input_data=ContainerInput([], "group", "slack:group", False, invocation_ts=7.0),
        cwd=tmp_path,
        project_root=tmp_path,
        on_output=AsyncMock(),
        timeout_seconds=30,
        env={},
        queue=queue,
        target=RuntimeTarget.from_binding("group", "slack:group"),
    )


def test_host_helpers_handle_unscoped_sessions_and_missing_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert host_execution.codex_thread_exists_in_host_runtime(None)
    assert host_execution.codex_thread_id("codex:thread-1") == "thread-1"

    monkeypatch.setattr(
        host_execution.workspace_config,
        "load_resolved_config",
        lambda _folder: type("Resolved", (), {"execution_mode": "host", "cwd": ""})(),
    )
    operations = HostRuntimeOperations(
        build_agent_environment=lambda **_kwargs: {},
        prepare_mcp=AsyncMock(),
        sessions_root=tmp_path / "sessions",
        project_root=tmp_path,
        gateway_port=4000,
        prepare_host_codex_home=lambda _folder, _plugins: tmp_path / ".codex",
        host_learning_vault=lambda _folder: None,
        resolve_routed_host_cwd=lambda *_args, **_kwargs: HostExecutionCwd(tmp_path),
    )

    assert (
        host_execution.host_execution_cwd("group", operations, repo_accesses=[], recovered=False)
        is None
    )


def test_existing_host_thread_is_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        host_execution,
        "codex_thread_exists_in_host_runtime",
        lambda *_args, **_kwargs: True,
    )

    assert host_execution.codex_thread_exists_in_host_runtime(
        "codex:gpt-5.5:thread-1", codex_home=tmp_path / ".codex"
    )


def test_host_environment_preserves_existing_git_ceiling(tmp_path: Path) -> None:
    operations = HostRuntimeOperations(
        build_agent_environment=lambda **_kwargs: {"GIT_CEILING_DIRECTORIES": "/parent"},
        prepare_mcp=AsyncMock(),
        sessions_root=tmp_path / "sessions",
        project_root=tmp_path,
        gateway_port=4000,
        prepare_host_codex_home=lambda _folder, _plugins: tmp_path / ".codex",
        host_learning_vault=lambda _folder: None,
        resolve_routed_host_cwd=lambda *_args, **_kwargs: HostExecutionCwd(tmp_path),
    )

    env = host_execution.host_agent_env_vars(
        is_admin=False, group_folder="group", operations=operations
    )

    assert env["GIT_CEILING_DIRECTORIES"].split(":")[-1] == "/parent"


def test_host_environment_preserves_local_gateway_urls(tmp_path: Path) -> None:
    operations = HostRuntimeOperations(
        build_agent_environment=lambda **_kwargs: {
            "OPENAI_BASE_URL": "http://localhost:4000",
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:3000/path",
        },
        prepare_mcp=AsyncMock(),
        sessions_root=tmp_path / "sessions",
        project_root=tmp_path,
        gateway_port=4000,
        prepare_host_codex_home=lambda _folder, _plugins: tmp_path / ".codex",
        host_learning_vault=lambda _folder: None,
        resolve_routed_host_cwd=lambda *_args, **_kwargs: HostExecutionCwd(tmp_path),
    )

    env = host_execution.host_agent_env_vars(
        is_admin=False, group_folder="group", operations=operations
    )

    assert env["OPENAI_BASE_URL"] == "http://localhost:4000"
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:3000/path"


def test_host_turn_returns_result_and_releases_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _Queue(pending=True)
    runner = AsyncMock(return_value="success")
    monkeypatch.setattr(host_execution, "run_host_input", runner)

    result = asyncio.run(host_execution.run_host_agent_turn(_request(tmp_path, queue)))

    assert result == "success"
    assert queue.released == [queue.lease]
    runner.assert_awaited_once()


def test_host_turn_releases_lease_when_runner_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _Queue()
    monkeypatch.setattr(
        host_execution,
        "run_host_input",
        AsyncMock(side_effect=RuntimeError("runner failed")),
    )

    with pytest.raises(RuntimeError, match="runner failed"):
        asyncio.run(host_execution.run_host_agent_turn(_request(tmp_path, queue)))

    assert queue.released == [queue.lease]
