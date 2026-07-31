"""Public status projection behavior for repository and Temporal edge cases."""

from __future__ import annotations

import contextlib
import subprocess  # noqa: S404 - subprocess.CompletedProcess models injected git results.
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.host.orchestrator.status import (
    collect_status,
    get_temporal_scheduler_status,
)
from pynchy.state import init_test_database
from tests.status_support import MockStatusDeps, _inert_status

if TYPE_CHECKING:
    from pathlib import Path

_S = "pynchy.host.orchestrator.status"


@dataclass(frozen=True)
class _RepoContext:
    root: Path
    worktrees_dir: Path


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


def _repo_deps(tmp_path: Path) -> tuple[MockStatusDeps, Path]:
    root = tmp_path / "repo"
    worktrees = root / "worktrees"
    worktrees.mkdir(parents=True)
    deps = MockStatusDeps(repo_slugs=("repo",))
    deps.git_status.get_repo_context.return_value = _RepoContext(root, worktrees)
    return deps, worktrees


@pytest.mark.asyncio
async def test_status_reports_a_repo_without_directory_worktrees(tmp_path: Path) -> None:
    deps, worktrees = _repo_deps(tmp_path)
    (worktrees / "README").write_text("not a worktree")

    with _inert_status():
        result = await collect_status(deps, time.monotonic())

    assert result["repos"]["repo"]["head_sha"] == "0000000"
    assert "worktrees" not in result["repos"]["repo"]

    (worktrees / "README").unlink()
    worktrees.rmdir()
    with _inert_status():
        without_directory = await collect_status(deps, time.monotonic())
    assert "worktrees" not in without_directory["repos"]["repo"]

    deps.git_status.get_repo_context.return_value = None
    with _inert_status():
        missing = await collect_status(deps, time.monotonic())
    assert missing["repos"] == {}


@pytest.mark.asyncio
async def test_status_reports_worktree_git_directory_failures_and_relative_paths(
    tmp_path: Path,
) -> None:
    deps, worktrees = _repo_deps(tmp_path)
    (worktrees / "thread").mkdir()

    def failed_conflict_check(*args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] == "rev-list":
            return subprocess.CompletedProcess(args, 0, stdout="0\n")
        raise OSError("git directory disappeared")

    deps.git_status.run_git.side_effect = failed_conflict_check
    with _inert_status():
        failed = await collect_status(deps, time.monotonic())

    assert failed["repos"]["repo"]["worktrees"]["thread"]["conflict"] is False

    def relative_conflict_check(
        *args: object, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "rev-list":
            return subprocess.CompletedProcess(args, 0, stdout="0\n")
        return subprocess.CompletedProcess(args, 0, stdout=".git\n")

    deps.git_status.run_git.side_effect = relative_conflict_check
    with _inert_status():
        relative = await collect_status(deps, time.monotonic())

    assert relative["repos"]["repo"]["worktrees"]["thread"]["conflict"] is False

    def nonzero_conflict_check(
        *args: object, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "rev-list":
            return subprocess.CompletedProcess(args, 0, stdout="0\n")
        return subprocess.CompletedProcess(args, 1, stdout="")

    deps.git_status.run_git.side_effect = nonzero_conflict_check
    with _inert_status():
        nonzero = await collect_status(deps, time.monotonic())
    assert nonzero["repos"]["repo"]["worktrees"]["thread"]["conflict"] is False

    def absolute_conflict_check(
        *args: object, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "rev-list":
            return subprocess.CompletedProcess(args, 0, stdout="0\n")
        return subprocess.CompletedProcess(args, 0, stdout=str(worktrees / "thread" / ".git"))

    deps.git_status.run_git.side_effect = absolute_conflict_check
    with _inert_status():
        absolute = await collect_status(deps, time.monotonic())
    assert absolute["repos"]["repo"]["worktrees"]["thread"]["conflict"] is False


@pytest.mark.asyncio
async def test_status_reports_temporal_health_failure() -> None:
    deps = MockStatusDeps()
    scheduler_status = {
        "worker_running": False,
        "last_workflow_id": None,
        "last_task_id": None,
        "last_result": None,
        "last_started_at": None,
        "last_completed_at": None,
        "last_error": None,
    }

    async def run_with_client(connect: AsyncMock) -> dict[str, Any]:
        patches: dict[str, Any] = {
            "get_router_state": AsyncMock(return_value=None),
            "get_messaging_stats": AsyncMock(return_value={}),
            "collect_capability_status": AsyncMock(return_value={}),
            "get_all_tasks": AsyncMock(return_value=[]),
            "get_task_run_logs": AsyncMock(return_value=[]),
            "get_all_host_jobs": AsyncMock(return_value=[]),
            "_get_temporal_orchestration_states": AsyncMock(return_value={}),
            "get_temporal_scheduler_status": lambda: scheduler_status,
            "Client.connect": connect,
        }
        with contextlib.ExitStack() as stack:
            for name, replacement in patches.items():
                stack.enter_context(patch(f"{_S}.{name}", replacement))
            stack.enter_context(patch("aiohttp.ClientSession", side_effect=RuntimeError("offline")))
            return await collect_status(deps, time.monotonic())

    client = AsyncMock()
    client.service_client.check_health = AsyncMock(return_value=True)
    healthy = await run_with_client(AsyncMock(return_value=client))
    result = await run_with_client(AsyncMock(side_effect=RuntimeError("Temporal unavailable")))

    assert healthy["temporal"]["cluster_healthy"] is True
    assert result["temporal"]["cluster_healthy"] is None
    assert result["temporal"]["cluster_error"] == "Temporal unavailable"


def test_temporal_scheduler_status_is_loaded_lazily() -> None:
    expected = {"worker_running": False}
    with patch(
        "pynchy.host.orchestrator.temporal.api.get_temporal_scheduler_status",
        return_value=expected,
    ):
        assert get_temporal_scheduler_status() == expected
