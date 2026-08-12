"""Public Temporal git-sync behavior through its scheduler dependency adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from unittest.mock import AsyncMock, patch

from conftest import make_settings

from pynchy.config.api import NotificationsConfig
from pynchy.deployments import DeployRevision
from pynchy.host.git_ops.api import RepoContext, sync_poll
from pynchy.host.orchestrator.api import ConfigRefreshResult, ConfigRefreshStatus
from pynchy.host.orchestrator.temporal import git_sync
from pynchy.state import (
    init_test_database,
    initialize_deployment_state,
    set_router_state,
)
from pynchy.workspace.api import WorkspaceProfile

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


@dataclass
class _ActivityDeps:
    workspaces: dict[str, WorkspaceProfile]
    broadcast_host_message: AsyncMock
    broadcast_system_notice: AsyncMock
    active_folders: frozenset[str] = frozenset()

    def sync_personalization(self, _project_root: Path) -> str:
        return "skipped"

    def active_worktree_folders(self) -> set[str]:
        return set(self.active_folders)


@dataclass(frozen=True)
class _DiskUsage:
    total: int
    used: int
    free: int


@dataclass
class _ExplicitSessionDeps(_ActivityDeps):
    active_session: bool = True

    def has_active_session(self, _group_folder: str) -> bool:
        return self.active_session


class _NotificationAdapter(Protocol):
    def has_active_session(self, group_folder: str) -> bool: ...

    async def broadcast_host_message(self, jid: str, text: str) -> None: ...

    async def broadcast_system_notice(self, jid: str, text: str) -> None: ...

    async def wake_worktree_conflict(self, jid: str) -> None: ...


def _admin_workspace() -> WorkspaceProfile:
    return WorkspaceProfile(
        jid="discord:admin",
        name="admin",
        folder="admin",
        trigger="always",
        is_admin=True,
    )


async def _run_host_offer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    admin_workspace: str | None,
    broadcast_host_message: AsyncMock,
    offer_update: AsyncMock | None = None,
    active_folders: frozenset[str] = frozenset(),
) -> tuple[str, _ActivityDeps]:
    await init_test_database()
    applied = DeployRevision("deployed-sha", "config")
    await initialize_deployment_state(applied)
    await set_router_state(
        git_sync.HOST_STATE_KEY,
        '{"last_origin_sha":"old-origin","deployed_sha":"deployed-sha",'
        '"config_hash":"config","local_head":"deployed-sha","offered_sha":""}',
    )
    workspace = _admin_workspace()
    deps = _ActivityDeps(
        workspaces={workspace.jid: workspace},
        broadcast_host_message=broadcast_host_message,
        broadcast_system_notice=AsyncMock(),
        active_folders=active_folders,
    )
    if offer_update is not None:
        deps.offer_update = offer_update
    monkeypatch.delenv("PYNCHY_RUNTIME_HARNESS", raising=False)
    monkeypatch.setattr(
        git_sync,
        "get_settings",
        lambda: make_settings(
            project_root=tmp_path,
            notifications=NotificationsConfig(admin_workspace=admin_workspace),
        ),
    )
    monkeypatch.setattr(git_sync, "_find_pynchy_repo_ctx", lambda *_args: None)
    monkeypatch.setattr(git_sync, "_check_local_head_drift", AsyncMock(return_value=False))
    monkeypatch.setattr(git_sync, "check_origin_drift", sync_poll.check_origin_drift)
    monkeypatch.setattr(sync_poll, "host_get_origin_main_sha", lambda _root: "new-origin")
    monkeypatch.setattr(
        git_sync,
        "refresh_host_config",
        AsyncMock(return_value=ConfigRefreshResult(ConfigRefreshStatus.UNCHANGED, "config")),
    )
    monkeypatch.setattr(git_sync, "_require_scheduler_deps", lambda: deps)

    return await git_sync.run_host_git_sync(), deps


async def test_host_git_sync_falls_back_to_admin_broadcast_for_update_offer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    broadcast = AsyncMock()

    result, _deps = await _run_host_offer(
        monkeypatch,
        tmp_path,
        admin_workspace="admin",
        broadcast_host_message=broadcast,
    )

    assert result == "idle"
    broadcast.assert_awaited_once()
    assert broadcast.await_args.args[0] == "discord:admin"
    assert "new-orig" in broadcast.await_args.args[1]


async def test_host_git_sync_suppresses_offer_without_admin_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    broadcast = AsyncMock()
    monkeypatch.setattr(
        git_sync.shutil,
        "disk_usage",
        lambda _path: _DiskUsage(total=100 << 30, used=99 << 30, free=1 << 30),
    )

    result, _deps = await _run_host_offer(
        monkeypatch,
        tmp_path,
        admin_workspace=None,
        broadcast_host_message=broadcast,
    )

    assert result == "idle"
    broadcast.assert_not_awaited()


async def test_host_git_sync_continues_when_disk_probe_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_disk_probe(_path: Path) -> _DiskUsage:
        raise OSError("probe unavailable")

    monkeypatch.setattr(git_sync.shutil, "disk_usage", fail_disk_probe)

    result, _deps = await _run_host_offer(
        monkeypatch,
        tmp_path,
        admin_workspace="admin",
        broadcast_host_message=AsyncMock(),
    )

    assert result == "idle"


async def test_host_git_sync_notifies_admin_once_when_disk_space_is_low(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        git_sync.shutil,
        "disk_usage",
        lambda _path: _DiskUsage(total=100 << 30, used=99 << 30, free=1 << 30),
    )
    broadcast = AsyncMock()
    result, _deps = await _run_host_offer(
        monkeypatch,
        tmp_path,
        admin_workspace="admin",
        broadcast_host_message=broadcast,
    )

    assert result == "idle"
    assert await git_sync.run_host_git_sync() == "idle"
    disk_alerts = [
        call.args[1] for call in broadcast.await_args_list if "disk space" in call.args[1]
    ]
    assert disk_alerts == [
        (
            "ERROR: Host disk space critically low: 1.0 GiB free (1.0%). "
            "Free space before scheduled work continues."
        )
    ]


async def test_host_git_sync_prunes_worktree_venvs_once_per_day(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pruned: list[tuple[Path, set[str]]] = []
    monkeypatch.setattr(
        git_sync,
        "prune_stale_worktree_venvs",
        lambda root, *, active_folders: pruned.append((root, active_folders)) or [],
    )

    result, _deps = await _run_host_offer(
        monkeypatch,
        tmp_path,
        admin_workspace="admin",
        broadcast_host_message=AsyncMock(),
        active_folders=frozenset({"busy"}),
    )

    assert result == "idle"
    assert await git_sync.run_host_git_sync() == "idle"
    assert pruned == [(tmp_path / "data" / "worktrees", {"busy"})]

    await set_router_state("worktree_venv_gc_last_run", "not-a-timestamp")
    assert await git_sync.run_host_git_sync() == "idle"
    assert pruned == [
        (tmp_path / "data" / "worktrees", {"busy"}),
        (tmp_path / "data" / "worktrees", {"busy"}),
    ]

    await set_router_state("worktree_venv_gc_last_run", "2026-08-11T00:00:00")
    assert await git_sync.run_host_git_sync() == "idle"
    assert pruned == [
        (tmp_path / "data" / "worktrees", {"busy"}),
        (tmp_path / "data" / "worktrees", {"busy"}),
        (tmp_path / "data" / "worktrees", {"busy"}),
    ]


async def test_host_git_sync_retries_when_update_offer_broadcast_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    broadcast = AsyncMock(side_effect=RuntimeError("temporary delivery failure"))

    result, _deps = await _run_host_offer(
        monkeypatch,
        tmp_path,
        admin_workspace="admin",
        broadcast_host_message=broadcast,
    )

    assert result == "idle"
    broadcast.assert_awaited_once()


async def test_host_git_sync_uses_explicit_update_offer_dependency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    offer_update = AsyncMock(return_value=True)

    result, _deps = await _run_host_offer(
        monkeypatch,
        tmp_path,
        admin_workspace="admin",
        broadcast_host_message=AsyncMock(),
        offer_update=offer_update,
    )

    assert result == "idle"
    offer_update.assert_awaited_once_with("discord:admin", "new-origin")


async def test_host_git_sync_initializes_state_when_no_state_is_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    await init_test_database()
    applied = DeployRevision("deployed-sha", "config")
    await initialize_deployment_state(applied)
    workspace = _admin_workspace()
    deps = _ActivityDeps({workspace.jid: workspace}, AsyncMock(), AsyncMock())
    monkeypatch.delenv("PYNCHY_RUNTIME_HARNESS", raising=False)
    monkeypatch.setattr(git_sync, "get_settings", lambda: make_settings(project_root=tmp_path))
    monkeypatch.setattr(git_sync, "host_get_origin_main_sha", lambda _root: "origin")
    monkeypatch.setattr(git_sync, "get_local_head_sha", lambda _root: "deployed-sha")
    monkeypatch.setattr(git_sync, "get_deploy_config_hash", lambda: "config")
    monkeypatch.setattr(git_sync, "_find_pynchy_repo_ctx", lambda *_args: None)
    monkeypatch.setattr(git_sync, "_check_local_head_drift", AsyncMock(return_value=False))
    monkeypatch.setattr(git_sync, "check_origin_drift", AsyncMock(return_value=False))
    monkeypatch.setattr(
        git_sync,
        "refresh_host_config",
        AsyncMock(return_value=ConfigRefreshResult(ConfigRefreshStatus.UNCHANGED, "config")),
    )
    monkeypatch.setattr(git_sync, "_require_scheduler_deps", lambda: deps)

    assert await git_sync.run_host_git_sync() == "idle"


async def test_external_git_sync_routes_adapter_notifications_and_session_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    await init_test_database()
    slug = "owner/external"
    repo_ctx = RepoContext(slug, tmp_path / "repo", tmp_path / "worktrees")
    host_message = AsyncMock()
    system_notice = AsyncMock()
    deps = _ActivityDeps({}, host_message, system_notice)
    notified: list[bool] = []

    async def notify(
        _exclude: str | None, adapter: _NotificationAdapter, _repo: RepoContext
    ) -> None:
        has_active = adapter.has_active_session("group")
        notified.append(has_active)
        await adapter.broadcast_host_message("jid", "host update")
        await adapter.broadcast_system_notice("jid", "system update")
        await adapter.wake_worktree_conflict("jid")

    with patch(
        "pynchy.host.orchestrator.temporal.scheduler.start_interactive_message_workflow",
        new_callable=AsyncMock,
    ) as wake:
        await set_router_state(f"temporal_git_sync_external_state:{slug}", "old-origin")
        monkeypatch.setattr(git_sync, "get_repo_context", lambda _slug: repo_ctx)
        monkeypatch.setattr(git_sync, "_require_scheduler_deps", lambda: deps)
        monkeypatch.setattr(git_sync, "git_env_with_token", lambda _slug: None)
        monkeypatch.setattr(
            git_sync,
            "probe_origin_main_sha",
            lambda _root, _env: sync_poll.GitOriginProbe(sha="new-origin", error=None),
        )
        monkeypatch.setattr(
            git_sync,
            "host_update_main_result",
            lambda _root, _env: sync_poll.GitUpdateResult(succeeded=True, error=None),
        )
        monkeypatch.setattr(git_sync, "get_local_head_sha", lambda _root: "new-head")
        monkeypatch.setattr(git_sync, "host_notify_worktree_updates", notify)

        assert await git_sync.run_external_git_sync(slug) == "synced"

    assert notified == [False]
    host_message.assert_awaited_once_with("jid", "host update")
    system_notice.assert_awaited_once_with("jid", "system update")
    wake.assert_awaited_once_with("jid")


async def test_external_git_sync_uses_explicit_session_state_from_dependencies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    await init_test_database()
    slug = "owner/explicit-session"
    repo_ctx = RepoContext(slug, tmp_path / "repo", tmp_path / "worktrees")
    deps = _ExplicitSessionDeps({}, AsyncMock(), AsyncMock())
    notified: list[bool] = []

    async def notify(_exclude: str | None, adapter: _NotificationAdapter, _repo: RepoContext):
        notified.append(adapter.has_active_session("group"))
        await adapter.broadcast_host_message("jid", "host update")

    await set_router_state(f"temporal_git_sync_external_state:{slug}", "old-origin")
    monkeypatch.setattr(git_sync, "get_repo_context", lambda _slug: repo_ctx)
    monkeypatch.setattr(git_sync, "_require_scheduler_deps", lambda: deps)
    monkeypatch.setattr(git_sync, "git_env_with_token", lambda _slug: None)
    monkeypatch.setattr(
        git_sync,
        "probe_origin_main_sha",
        lambda _root, _env: sync_poll.GitOriginProbe(sha="new-origin", error=None),
    )
    monkeypatch.setattr(
        git_sync,
        "host_update_main_result",
        lambda _root, _env: sync_poll.GitUpdateResult(succeeded=True, error=None),
    )
    monkeypatch.setattr(git_sync, "get_local_head_sha", lambda _root: "new-head")
    monkeypatch.setattr(git_sync, "host_notify_worktree_updates", notify)

    assert await git_sync.run_external_git_sync(slug) == "synced"
    assert notified == [True]
