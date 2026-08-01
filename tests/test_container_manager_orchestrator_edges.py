"""Public agent-spawn behavior at the container orchestration boundary."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.agent_protocol.api import ContainerInput
from pynchy.host.container_manager.api import McpStartupFailure, RepoMountResolution
from pynchy.host.container_manager.mcp.startup import McpWorkspaceStartup
from pynchy.host.container_manager.orchestrator import configure_container_spawn_runtime
from pynchy.host.git_ops.api import RepoContext, WorktreeResult
from pynchy.workspace.api import RuntimeTarget
from tests.app_integration_support import (
    FakeProcess,
    _completed_awaitable,
    _patch_test_settings,
)

pytest_plugins = ("tests.app_integration_support",)

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from pathlib import Path

    from pynchy.host.orchestrator.app import PynchyApp


_CR_ORCH = "pynchy.host.container_manager.orchestrator"


@pytest.mark.parametrize(
    ("resolution_notices", "extra_system_notices", "expected_notices"),
    [
        (
            ("repository recovered with local changes",),
            None,
            ["repository recovered with local changes"],
        ),
        (
            ("repository recovered with local changes",),
            ["existing system notice"],
            ["existing system notice", "repository recovered with local changes"],
        ),
        ((), ["existing system notice"], ["existing system notice"]),
    ],
)
async def test_agent_forwards_repo_resolution_notices_without_mcp_routes(
    app: PynchyApp,
    tmp_path: Path,
    resolution_notices: tuple[str, ...],
    extra_system_notices: list[str] | None,
    expected_notices: list[str],
):
    fake_proc = FakeProcess(
        output={
            "status": "success",
            "result": "repo notice handled",
            "new_session_id": "s-repo-notice",
        }
    )
    driver = asyncio.create_task(fake_proc.schedule_output())

    def fake_create(*args: Any, **kwargs: Any) -> Awaitable[FakeProcess]:
        return _completed_awaitable(fake_proc)

    configure_container_spawn_runtime(
        container_cli="docker",
        ensure_agent_image=lambda **_kwargs: None,
        resolve_repo_mounts=lambda _folder, _repos: RepoMountResolution(notices=resolution_notices),
    )
    group = app.workspaces["group@g.us"]

    with (
        patch(f"{_CR_ORCH}.asyncio.create_subprocess_exec", fake_create),
        _patch_test_settings(tmp_path),
        patch(
            "pynchy.host.container_manager.mcp.manager.get_mcp_manager",
            return_value=MagicMock(
                get_workspace_instance_ids=MagicMock(return_value=[]),
                ensure_workspace_running=AsyncMock(return_value=McpWorkspaceStartup((), ())),
                get_direct_server_configs=MagicMock(return_value=[]),
            ),
        ),
    ):
        (tmp_path / "groups" / "test-group").mkdir(parents=True)
        result = await app.queue.run_serialized_task(
            RuntimeTarget.from_workspace(group),
            "test-run-repo-notice",
            lambda: app.run_agent(
                group,
                "group@g.us",
                [{"message_type": "user", "content": "check repository"}],
                extra_system_notices=extra_system_notices,
                repo_access_override="owner/pynchy",
            ),
        )

    await driver
    assert result == "success"
    initial_input = json.loads(
        (tmp_path / "data" / "ipc" / "test-group" / "input" / "initial.json").read_text()
    )
    assert initial_input["system_notices"] == expected_notices


@pytest.mark.parametrize("repository_available", [True, False])
async def test_agent_resolves_repo_mounts_through_the_public_spawn_path(
    app: PynchyApp,
    tmp_path: Path,
    repository_available: bool,
) -> None:
    fake_proc = FakeProcess(
        output={
            "status": "success",
            "result": "repo mounted",
            "new_session_id": "s-repo-mounted",
        }
    )
    driver = asyncio.create_task(fake_proc.schedule_output())
    repo = RepoContext(
        slug="owner/pynchy",
        root=tmp_path / "repo",
        worktrees_dir=tmp_path / "worktrees",
    )
    worktree = WorktreeResult(
        path=repo.worktrees_dir / "test-group",
        notices=["repository recovered with local changes"],
    )

    def fake_create(*args: Any, **kwargs: Any) -> Awaitable[FakeProcess]:
        return _completed_awaitable(fake_proc)

    with (
        patch(f"{_CR_ORCH}.asyncio.create_subprocess_exec", fake_create),
        _patch_test_settings(tmp_path),
        patch(
            "pynchy.host.orchestrator.app.get_repo_context",
            return_value=repo if repository_available else None,
        ),
        patch("pynchy.host.orchestrator.app.ensure_worktree", return_value=worktree),
    ):
        (tmp_path / "groups" / "test-group").mkdir(parents=True)
        group = app.workspaces["group@g.us"]
        result = await app.queue.run_serialized_task(
            RuntimeTarget.from_workspace(group),
            "test-run-repo-mount",
            lambda: app.run_agent(
                group,
                "group@g.us",
                [{"message_type": "user", "content": "check repository"}],
                repo_access_override=repo.slug,
            ),
        )

    await driver
    assert result == "success"
    initial_input = json.loads(
        (tmp_path / "data" / "ipc" / "test-group" / "input" / "initial.json").read_text()
    )
    assert initial_input["repo_accesses"] == [repo.slug]
    assert initial_input.get("system_notices", []) == (
        list(worktree.notices) if repository_available else []
    )


async def test_app_skips_optional_mcp_start_when_manager_is_unconfigured(
    app: PynchyApp, monkeypatch
) -> None:
    monkeypatch.setattr(
        "pynchy.host.container_manager.mcp.manager.get_mcp_manager",
        lambda: None,
    )

    assert await app.container_agent_operations.ensure_workspace_mcp("test-group") == ()


async def test_app_ensures_optional_mcp_start_when_manager_is_configured(
    app: PynchyApp, monkeypatch
) -> None:
    manager = MagicMock()
    manager.ensure_workspace_running = AsyncMock(
        return_value=McpWorkspaceStartup(("docs-instance",), ())
    )
    monkeypatch.setattr(
        "pynchy.host.orchestrator.app.get_mcp_manager",
        lambda: manager,
    )

    assert await app.container_agent_operations.ensure_workspace_mcp("test-group") == ()
    manager.ensure_workspace_running.assert_awaited_once_with("test-group")


async def test_agent_requires_composed_container_spawn_runtime(app, monkeypatch) -> None:
    group = app.workspaces["group@g.us"]
    monkeypatch.setattr(f"{_CR_ORCH}._container_cli", None)

    assert (
        await app.run_agent(
            group,
            "group@g.us",
            [{"message_type": "user", "content": "check runtime"}],
        )
        == "error"
    )


async def test_host_mcp_preparation_skips_when_manager_is_unconfigured(
    app: PynchyApp, monkeypatch
) -> None:
    monkeypatch.setattr("pynchy.host.orchestrator.app.get_mcp_manager", lambda: None)
    input_data = ContainerInput([], "test-group", "group@g.us", False)

    await app.host_runtime_operations.prepare_mcp(
        input_data,
        "test-group",
        "group@g.us",
        AsyncMock(),
    )

    assert input_data.mcp_direct_servers is None


async def test_host_mcp_preparation_notifies_failures_and_attaches_routes(
    app: PynchyApp, monkeypatch
) -> None:
    failure = McpStartupFailure(
        instance_id="docs-instance",
        server_name="docs",
        reason="start timed out",
    )
    manager = MagicMock()
    manager.ensure_workspace_running = AsyncMock(
        return_value=McpWorkspaceStartup(("docs-instance",), (failure,))
    )
    manager.get_direct_server_configs.return_value = [
        {"name": "docs", "url": "http://mcp-docs:8000", "transport": "sse"}
    ]
    monkeypatch.setattr("pynchy.host.orchestrator.app.get_mcp_manager", lambda: manager)
    input_data = ContainerInput([], "test-group", "group@g.us", False)
    broadcast = AsyncMock()

    await app.host_runtime_operations.prepare_mcp(
        input_data,
        "test-group",
        "group@g.us",
        broadcast,
    )

    assert input_data.mcp_direct_servers == manager.get_direct_server_configs.return_value
    manager.ensure_workspace_running.assert_awaited_once_with("test-group")
    manager.get_direct_server_configs.assert_called_once_with(
        "test-group",
        invocation_ts=input_data.invocation_ts,
        instance_ids=("docs-instance",),
    )
    broadcast.assert_awaited_once()


async def test_host_mcp_preparation_attaches_routes_without_failures(
    app: PynchyApp, monkeypatch
) -> None:
    manager = MagicMock()
    manager.ensure_workspace_running = AsyncMock(
        return_value=McpWorkspaceStartup(("docs-instance",), ())
    )
    manager.get_direct_server_configs.return_value = [
        {"name": "docs", "url": "http://mcp-docs:8000", "transport": "sse"}
    ]
    monkeypatch.setattr("pynchy.host.orchestrator.app.get_mcp_manager", lambda: manager)
    input_data = ContainerInput([], "test-group", "group@g.us", False)
    broadcast = AsyncMock()

    await app.host_runtime_operations.prepare_mcp(
        input_data,
        "test-group",
        "group@g.us",
        broadcast,
    )

    assert input_data.mcp_direct_servers == manager.get_direct_server_configs.return_value
    broadcast.assert_not_awaited()
