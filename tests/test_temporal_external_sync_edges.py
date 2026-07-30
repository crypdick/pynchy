"""Public external Temporal git-sync state transitions."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from conftest import init_test_database

from pynchy.host.git_ops.api import RepoContext, sync_poll
from pynchy.host.orchestrator.temporal import git_sync
from pynchy.state import set_router_state

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


async def test_missing_external_repo_is_skipped(monkeypatch: pytest.MonkeyPatch):
    await init_test_database()
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(git_sync, "get_repo_context", lambda _slug: None)
    monkeypatch.setattr(
        git_sync,
        "_record_tracked_activity_result",
        lambda task_id, result: recorded.append((task_id, result)),
    )

    assert await git_sync.run_external_git_sync("owner/missing") == "skipped"
    assert recorded == [("git-sync-repo:owner/missing", "skipped")]


async def test_first_external_origin_is_initialized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    await init_test_database()
    repo = RepoContext("owner/project", tmp_path / "repo", tmp_path / "worktrees")
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(git_sync, "get_repo_context", lambda _slug: repo)
    monkeypatch.setattr(git_sync, "_require_scheduler_deps", object)
    monkeypatch.setattr(git_sync, "git_env_with_token", lambda _slug: None)
    monkeypatch.setattr(
        git_sync,
        "probe_origin_main_sha",
        lambda _root, _env: sync_poll.GitOriginProbe(sha="origin-1"),
    )
    monkeypatch.setattr(
        git_sync,
        "_record_tracked_activity_result",
        lambda task_id, result: recorded.append((task_id, result)),
    )

    assert await git_sync.run_external_git_sync(repo.slug) == "initialized"
    assert recorded == [("git-sync-repo:owner/project", "initialized")]


async def test_unchanged_external_origin_is_idle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    await init_test_database()
    repo = RepoContext("owner/project", tmp_path / "repo", tmp_path / "worktrees")
    await set_router_state("temporal_git_sync_external_state:owner/project", "origin-1")
    monkeypatch.setattr(git_sync, "get_repo_context", lambda _slug: repo)
    monkeypatch.setattr(git_sync, "_require_scheduler_deps", object)
    monkeypatch.setattr(git_sync, "git_env_with_token", lambda _slug: None)
    monkeypatch.setattr(
        git_sync,
        "probe_origin_main_sha",
        lambda _root, _env: sync_poll.GitOriginProbe(sha="origin-1"),
    )

    assert await git_sync.run_external_git_sync(repo.slug) == "idle"


async def test_changed_external_origin_updates_and_notifies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    await init_test_database()
    repo = RepoContext("owner/project", tmp_path / "repo", tmp_path / "worktrees")
    await set_router_state("temporal_git_sync_external_state:owner/project", "origin-1")
    notify = AsyncMock()
    monkeypatch.setattr(git_sync, "get_repo_context", lambda _slug: repo)
    monkeypatch.setattr(git_sync, "_require_scheduler_deps", object)
    monkeypatch.setattr(git_sync, "git_env_with_token", lambda _slug: None)
    monkeypatch.setattr(
        git_sync,
        "probe_origin_main_sha",
        lambda _root, _env: sync_poll.GitOriginProbe(sha="origin-2"),
    )
    monkeypatch.setattr(
        git_sync,
        "host_update_main_result",
        lambda _root, _env: sync_poll.GitUpdateResult(succeeded=True),
    )
    monkeypatch.setattr(git_sync, "get_local_head_sha", lambda _root: "head-2")
    monkeypatch.setattr(git_sync, "host_notify_worktree_updates", notify)
    git_sync.last_notified_sha.clear()

    assert await git_sync.run_external_git_sync(repo.slug) == "synced"
    notify.assert_awaited_once()
