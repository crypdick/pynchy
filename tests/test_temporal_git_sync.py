"""Tests for the Temporal host-repository synchronization activity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from conftest import make_settings
from temporalio.exceptions import ApplicationError

import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler
from pynchy.config.api import NotificationsConfig
from pynchy.deployments import DeployRevision
from pynchy.host.git_ops.api import RepoContext, check_local_head_drift, sync_poll
from pynchy.host.orchestrator.api import ConfigRefreshResult, ConfigRefreshStatus
from pynchy.host.orchestrator.temporal import git_sync
from pynchy.host.orchestrator.temporal.runtime_state import get_temporal_scheduler_status
from pynchy.state import (
    claim_deployment,
    complete_deployment,
    init_test_database,
    initialize_deployment_state,
    set_router_state,
)
from pynchy.workspace.api import WorkspaceProfile

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _RuntimeDeps:
    """The scheduler-dependency subset used by the git-sync Temporal adapter."""

    workspaces: dict[str, WorkspaceProfile]
    broadcast_host_message: object

    def sync_personalization(self, _project_root: Path) -> str:
        return "skipped"


@dataclass
class _UpdateDeps:
    """Minimal git-sync dependency object that records update outcomes."""

    offer_update: AsyncMock
    trigger_deploy: AsyncMock

    async def broadcast_host_message(self, _jid: str, _text: str) -> None: ...

    async def broadcast_system_notice(self, _jid: str, _text: str) -> None: ...

    async def wake_worktree_conflict(self, _jid: str) -> None: ...

    def has_active_session(self, _group_folder: str) -> bool:
        return False

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return {}


async def test_host_git_sync_skips_the_hermetic_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generated runtime must not deploy-restart its unsupervised process."""
    monkeypatch.setenv("PYNCHY_RUNTIME_HARNESS", "1")
    called = False

    def require_scheduler_deps() -> object:
        nonlocal called
        called = True
        return object()

    monkeypatch.setattr(git_sync, "_require_scheduler_deps", require_scheduler_deps)
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        git_sync,
        "_record_tracked_activity_result",
        lambda task_id, result: recorded.append((task_id, result)),
    )

    assert await git_sync.run_host_git_sync() == "skipped"
    assert not called
    assert recorded == [(git_sync.HOST_GIT_SYNC_ID, "skipped")]


async def test_host_git_sync_reinitializes_corrupt_persisted_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await init_test_database()
    await set_router_state(git_sync.HOST_STATE_KEY, "not-json")
    monkeypatch.delenv("PYNCHY_RUNTIME_HARNESS", raising=False)
    monkeypatch.setattr(git_sync, "get_settings", lambda: make_settings(project_root=tmp_path))
    monkeypatch.setattr(git_sync, "host_get_origin_main_sha", lambda _root: "origin")
    monkeypatch.setattr(git_sync, "get_local_head_sha", lambda _root: "local")
    monkeypatch.setattr(git_sync, "get_deploy_config_hash", lambda: "config")
    monkeypatch.setattr(git_sync, "_find_pynchy_repo_ctx", lambda *_args: None)
    monkeypatch.setattr(git_sync, "_check_local_head_drift", AsyncMock(return_value=False))
    monkeypatch.setattr(git_sync, "check_origin_drift", AsyncMock(return_value=False))
    monkeypatch.setattr(
        git_sync,
        "refresh_host_config",
        AsyncMock(return_value=ConfigRefreshResult(ConfigRefreshStatus.UNCHANGED, "config")),
    )
    monkeypatch.setattr(
        git_sync,
        "_require_scheduler_deps",
        lambda: _RuntimeDeps(workspaces={}, broadcast_host_message=AsyncMock()),
    )

    assert await git_sync.run_host_git_sync() == "idle"
    assert await git_sync.get_router_state(git_sync.HOST_STATE_KEY)


@pytest.mark.parametrize(
    ("sync_result", "expected"),
    [
        ("pushed", "personalization_pushed"),
        ("updated", "personalization_updated"),
    ],
)
async def test_host_git_sync_reports_personalization_sync(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    sync_result: str,
    expected: str,
) -> None:
    await init_test_database()
    applied = DeployRevision("deployed-sha", "config")
    await initialize_deployment_state(applied)
    await set_router_state(
        git_sync.HOST_STATE_KEY,
        '{"last_origin_sha":"origin","deployed_sha":"deployed-sha",'
        '"config_hash":"config","local_head":"deployed-sha"}',
    )
    monkeypatch.delenv("PYNCHY_RUNTIME_HARNESS", raising=False)
    monkeypatch.setattr(git_sync, "get_settings", lambda: make_settings(project_root=tmp_path))
    monkeypatch.setattr(git_sync, "_find_pynchy_repo_ctx", lambda *_args: None)
    monkeypatch.setattr(git_sync, "_check_local_head_drift", AsyncMock(return_value=False))
    monkeypatch.setattr(git_sync, "check_origin_drift", AsyncMock(return_value=False))
    monkeypatch.setattr(
        git_sync,
        "refresh_host_config",
        AsyncMock(return_value=ConfigRefreshResult(ConfigRefreshStatus.UNCHANGED, "config")),
    )
    deps = _RuntimeDeps(workspaces={}, broadcast_host_message=AsyncMock())
    monkeypatch.setattr(deps, "sync_personalization", lambda _root: sync_result)
    monkeypatch.setattr(git_sync, "_require_scheduler_deps", lambda: deps)

    assert await git_sync.run_host_git_sync() == expected


async def test_trigger_deploy_reports_workflow_start_failure_after_rolling_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """A config drift + Temporal outage rolls back rather than leaving a changed checkout."""
    await init_test_database()
    previous = DeployRevision("old-sha", "old-config")
    await initialize_deployment_state(previous)
    await set_router_state(
        git_sync.HOST_STATE_KEY,
        '{"last_origin_sha":"old-sha","deployed_sha":"old-sha",'
        '"config_hash":"old-config","local_head":"old-sha"}',
    )
    workspace = WorkspaceProfile(
        jid="discord:admin",
        name="admin",
        folder="admin",
        trigger="always",
        is_admin=True,
    )
    runtime_deps = _RuntimeDeps(
        workspaces={workspace.jid: workspace},
        broadcast_host_message=AsyncMock(),
    )
    report_failure = AsyncMock(return_value="workflow_start_failed_rolled_back")

    monkeypatch.setattr(
        git_sync,
        "get_settings",
        lambda: make_settings(
            project_root=tmp_path,
            notifications=NotificationsConfig(admin_workspace=None),
        ),
    )
    monkeypatch.setattr(git_sync, "get_local_head_sha", lambda _root: "new-sha")
    monkeypatch.setattr(git_sync, "get_deploy_config_hash", lambda: "new-config")
    monkeypatch.setattr(
        git_sync,
        "refresh_host_config",
        AsyncMock(
            return_value=ConfigRefreshResult(
                ConfigRefreshStatus.RESTART_REQUIRED,
                "new-config",
            )
        ),
    )
    monkeypatch.setattr(git_sync, "_require_scheduler_deps", lambda: runtime_deps)
    monkeypatch.setattr(
        temporal_scheduler,
        "start_deploy_workflow",
        AsyncMock(side_effect=RuntimeError("Temporal unavailable")),
    )
    monkeypatch.setattr(
        git_sync,
        "rollback_and_report_failure",
        report_failure,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="Temporal unavailable"):
        await git_sync.run_host_git_sync()

    report_failure.assert_awaited_once()
    request = report_failure.await_args.kwargs["request"]
    assert request.commit_sha == "new-sha"
    assert request.previous_sha == "old-sha"
    assert report_failure.await_args.kwargs["failure_result"] == "workflow_start_failed"


async def test_applied_revision_overrides_stale_sync_snapshot_after_http_deploy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """A stale poll snapshot cannot redeploy the revision that just booted."""
    await init_test_database()
    previous = DeployRevision("old-sha", "config-a")
    deployed = DeployRevision("new-sha", "config-b")
    await initialize_deployment_state(previous)
    await claim_deployment(deployed)
    await complete_deployment(deployed)
    await set_router_state(
        git_sync.HOST_STATE_KEY,
        '{"last_origin_sha":"new-sha","deployed_sha":"old-sha",'
        '"config_hash":"config-a","local_head":"new-sha"}',
    )
    monkeypatch.delenv("PYNCHY_RUNTIME_HARNESS", raising=False)
    monkeypatch.setattr(
        git_sync,
        "get_settings",
        lambda: make_settings(project_root=tmp_path),
    )
    monkeypatch.setattr(git_sync, "get_local_head_sha", lambda _root: deployed.commit_sha)
    monkeypatch.setattr(git_sync, "get_deploy_config_hash", lambda: deployed.config_hash)
    monkeypatch.setattr(git_sync, "_find_pynchy_repo_ctx", lambda *_args: None)
    monkeypatch.setattr(git_sync, "check_origin_drift", AsyncMock(return_value=False))
    runtime_deps = _RuntimeDeps(workspaces={}, broadcast_host_message=AsyncMock())
    monkeypatch.setattr(git_sync, "_require_scheduler_deps", lambda: runtime_deps)
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        git_sync,
        "_record_tracked_activity_result",
        lambda task_id, result: recorded.append((task_id, result)),
    )

    assert await git_sync.run_host_git_sync() == "idle"
    assert recorded == [(git_sync.HOST_GIT_SYNC_ID, "idle")]


async def test_host_git_sync_passes_shared_state_to_git_adapter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Temporal and the Git adapter must use one runtime-checked state contract."""
    await init_test_database()
    applied = DeployRevision("deployed-sha", "restart-hash")
    await initialize_deployment_state(applied)
    await set_router_state(
        git_sync.HOST_STATE_KEY,
        '{"last_origin_sha":"origin","deployed_sha":"deployed-sha",'
        '"config_hash":"restart-hash","local_head":"deployed-sha","offered_sha":""}',
    )
    monkeypatch.delenv("PYNCHY_RUNTIME_HARNESS", raising=False)
    monkeypatch.setattr(git_sync, "get_settings", lambda: make_settings(project_root=tmp_path))
    monkeypatch.setattr(git_sync, "_find_pynchy_repo_ctx", lambda *_args: None)
    monkeypatch.setattr(git_sync, "_check_local_head_drift", check_local_head_drift)
    monkeypatch.setattr(git_sync, "check_origin_drift", AsyncMock(return_value=False))
    monkeypatch.setattr(sync_poll, "get_local_head_sha", lambda _root: applied.commit_sha)
    monkeypatch.setattr(
        git_sync,
        "refresh_host_config",
        AsyncMock(
            return_value=ConfigRefreshResult(
                ConfigRefreshStatus.UNCHANGED,
                applied.config_hash,
            )
        ),
    )
    monkeypatch.setattr(
        git_sync,
        "_require_scheduler_deps",
        lambda: _RuntimeDeps(workspaces={}, broadcast_host_message=AsyncMock()),
    )

    assert await git_sync.run_host_git_sync() == "idle"


async def test_host_git_sync_publishes_skill_policy_without_deploy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await init_test_database()
    applied = DeployRevision("deployed-sha", "restart-hash")
    await initialize_deployment_state(applied)
    await set_router_state(
        git_sync.HOST_STATE_KEY,
        '{"last_origin_sha":"origin","deployed_sha":"deployed-sha",'
        '"config_hash":"restart-hash","local_head":"deployed-sha","offered_sha":""}',
    )
    monkeypatch.delenv("PYNCHY_RUNTIME_HARNESS", raising=False)
    monkeypatch.setattr(git_sync, "get_settings", lambda: make_settings(project_root=tmp_path))
    monkeypatch.setattr(git_sync, "_find_pynchy_repo_ctx", lambda *_args: None)
    monkeypatch.setattr(git_sync, "_check_local_head_drift", AsyncMock(return_value=False))
    monkeypatch.setattr(git_sync, "check_origin_drift", AsyncMock(return_value=False))
    refresh = AsyncMock(
        return_value=ConfigRefreshResult(
            ConfigRefreshStatus.REFRESHED,
            applied.config_hash,
        )
    )
    monkeypatch.setattr(git_sync, "refresh_host_config", refresh)
    monkeypatch.setattr(
        git_sync,
        "_require_scheduler_deps",
        lambda: _RuntimeDeps(workspaces={}, broadcast_host_message=AsyncMock()),
    )

    assert await git_sync.run_host_git_sync() == "config_refreshed"
    refresh.assert_awaited_once_with(applied.config_hash)


async def test_host_git_sync_uses_restart_hash_from_validated_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await init_test_database()
    applied = DeployRevision("deployed-sha", "old-restart-hash")
    await initialize_deployment_state(applied)
    await set_router_state(
        git_sync.HOST_STATE_KEY,
        '{"last_origin_sha":"origin","deployed_sha":"deployed-sha",'
        '"config_hash":"old-restart-hash","local_head":"deployed-sha","offered_sha":""}',
    )
    monkeypatch.delenv("PYNCHY_RUNTIME_HARNESS", raising=False)
    monkeypatch.setattr(git_sync, "get_settings", lambda: make_settings(project_root=tmp_path))
    monkeypatch.setattr(git_sync, "_find_pynchy_repo_ctx", lambda *_args: None)
    monkeypatch.setattr(git_sync, "_check_local_head_drift", AsyncMock(return_value=False))
    monkeypatch.setattr(git_sync, "check_origin_drift", AsyncMock(return_value=False))
    monkeypatch.setattr(
        git_sync,
        "refresh_host_config",
        AsyncMock(
            return_value=ConfigRefreshResult(
                ConfigRefreshStatus.RESTART_REQUIRED,
                "new-restart-hash",
            )
        ),
    )
    start_deploy = AsyncMock()
    monkeypatch.setattr(temporal_scheduler, "start_deploy_workflow", start_deploy)
    monkeypatch.setattr(
        git_sync,
        "_require_scheduler_deps",
        lambda: _RuntimeDeps(workspaces={}, broadcast_host_message=AsyncMock()),
    )

    assert await git_sync.run_host_git_sync() == "deploy_started"
    start_deploy.assert_awaited_once()
    request = start_deploy.await_args.args[0]
    assert request.previous_sha == applied.commit_sha
    assert request.rebuild is False
    assert request.config_hash == "new-restart-hash"


@pytest.mark.parametrize("suppression", ["active", "pending", "approval"])
async def test_code_deployment_state_suppresses_config_refresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    suppression: str,
) -> None:
    await init_test_database()
    applied = DeployRevision("deployed-sha", "restart-hash")
    await initialize_deployment_state(applied)
    local_head = "local-code" if suppression == "approval" else applied.commit_sha
    await set_router_state(
        git_sync.HOST_STATE_KEY,
        '{"last_origin_sha":"origin","deployed_sha":"deployed-sha",'
        f'"config_hash":"restart-hash","local_head":"{local_head}","offered_sha":""}}',
    )
    if suppression == "pending":
        await claim_deployment(DeployRevision("pending-code", "restart-hash"))

    monkeypatch.delenv("PYNCHY_RUNTIME_HARNESS", raising=False)
    monkeypatch.setattr(git_sync, "get_settings", lambda: make_settings(project_root=tmp_path))
    monkeypatch.setattr(git_sync, "_find_pynchy_repo_ctx", lambda *_args: None)
    monkeypatch.setattr(
        git_sync,
        "_check_local_head_drift",
        AsyncMock(return_value=suppression == "active"),
    )
    monkeypatch.setattr(git_sync, "check_origin_drift", AsyncMock(return_value=False))
    monkeypatch.setattr(git_sync, "needs_deploy", lambda _old, _new: True)
    refresh = AsyncMock()
    monkeypatch.setattr(git_sync, "refresh_host_config", refresh)
    monkeypatch.setattr(
        git_sync,
        "_require_scheduler_deps",
        lambda: _RuntimeDeps(workspaces={}, broadcast_host_message=AsyncMock()),
    )

    result = await git_sync.run_host_git_sync()

    assert (
        result
        == {
            "active": "deploy_started",
            "pending": "deploy_pending",
            "approval": "update_pending",
        }[suppression]
    )
    refresh.assert_not_awaited()


async def test_external_git_sync_unavailable_fails_temporal_and_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await init_test_database()
    slug = "owner/unavailable"
    repo_ctx = RepoContext(
        slug=slug,
        root=tmp_path / "repo",
        worktrees_dir=tmp_path / "worktrees",
    )
    diagnostic = "git ls-remote origin refs/heads/main failed with exit 128: access denied"
    monkeypatch.setattr(git_sync, "get_repo_context", lambda _slug: repo_ctx)
    monkeypatch.setattr(git_sync, "_require_scheduler_deps", object)
    monkeypatch.setattr(git_sync, "git_env_with_token", lambda _slug: None)
    monkeypatch.setattr(
        git_sync,
        "probe_origin_main_sha",
        lambda _root, _env: sync_poll.GitOriginProbe(sha=None, error=diagnostic),
    )

    with pytest.raises(ApplicationError) as raised:
        await git_sync.run_external_git_sync(slug)

    assert raised.value.type == "ExternalGitSyncUnavailable"
    assert raised.value.non_retryable
    status = get_temporal_scheduler_status()
    task_id = f"{git_sync.EXTERNAL_GIT_SYNC_PREFIX}{slug}"
    assert status["last_task_id"] == task_id
    assert status["last_result"] == "unavailable"
    assert status["last_error"] == f"External git sync unavailable for {slug}: {diagnostic}"
    assert status["tracked_results"][task_id]["result"] == "unavailable"
    assert status["tracked_results"][task_id]["error"] == status["last_error"]


async def test_external_git_sync_update_failure_fails_temporal_and_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await init_test_database()
    slug = "owner/sync-failure"
    repo_ctx = RepoContext(
        slug=slug,
        root=tmp_path / "repo",
        worktrees_dir=tmp_path / "worktrees",
    )
    diagnostic = "git fetch origin failed with exit 128: access denied"
    monkeypatch.setattr(git_sync, "get_repo_context", lambda _slug: repo_ctx)
    monkeypatch.setattr(git_sync, "_require_scheduler_deps", object)
    monkeypatch.setattr(git_sync, "git_env_with_token", lambda _slug: None)
    monkeypatch.setattr(
        git_sync,
        "_load_external_origin",
        AsyncMock(return_value="old-origin"),
    )
    monkeypatch.setattr(
        git_sync,
        "probe_origin_main_sha",
        lambda _root, _env: sync_poll.GitOriginProbe(sha="new-origin"),
    )
    monkeypatch.setattr(
        git_sync,
        "host_update_main_result",
        lambda _root, _env: sync_poll.GitUpdateResult(
            succeeded=False,
            error=diagnostic,
        ),
    )

    with pytest.raises(ApplicationError) as raised:
        await git_sync.run_external_git_sync(slug)

    assert raised.value.type == "ExternalGitSyncFailed"
    assert raised.value.non_retryable
    status = get_temporal_scheduler_status()
    task_id = f"{git_sync.EXTERNAL_GIT_SYNC_PREFIX}{slug}"
    assert status["last_task_id"] == task_id
    assert status["last_result"] == "sync_failed"
    assert status["last_error"] == f"External git sync sync failed for {slug}: {diagnostic}"
    assert status["tracked_results"][task_id]["result"] == "sync_failed"
    assert status["tracked_results"][task_id]["error"] == status["last_error"]


@pytest.mark.parametrize("already_notified", [False, True])
async def test_external_git_sync_notifies_after_a_successful_update(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    already_notified: bool,
) -> None:
    await init_test_database()
    slug = "owner/synced"
    repo_ctx = RepoContext(
        slug=slug,
        root=tmp_path / "repo",
        worktrees_dir=tmp_path / "worktrees",
    )
    deps = _RuntimeDeps(workspaces={}, broadcast_host_message=AsyncMock())
    notify = AsyncMock()
    monkeypatch.setattr(git_sync, "get_repo_context", lambda _slug: repo_ctx)
    monkeypatch.setattr(git_sync, "_require_scheduler_deps", lambda: deps)
    monkeypatch.setattr(git_sync, "git_env_with_token", lambda _slug: None)
    monkeypatch.setattr(git_sync, "_load_external_origin", AsyncMock(return_value="old-origin"))
    monkeypatch.setattr(
        git_sync,
        "probe_origin_main_sha",
        lambda _root, _env: sync_poll.GitOriginProbe(sha="new-origin"),
    )
    monkeypatch.setattr(
        git_sync,
        "host_update_main_result",
        lambda _root, _env: sync_poll.GitUpdateResult(succeeded=True),
    )
    monkeypatch.setattr(git_sync, "get_local_head_sha", lambda _root: "new-head")
    monkeypatch.setattr(git_sync, "host_notify_worktree_updates", notify)
    monkeypatch.setattr(
        git_sync,
        "last_notified_sha",
        {str(repo_ctx.root): "new-head"} if already_notified else {},
    )

    assert await git_sync.run_external_git_sync(slug) == "synced"
    if already_notified:
        notify.assert_not_awaited()
    else:
        notify.assert_awaited_once()
        assert notify.await_args.args[0] is None
        assert notify.await_args.args[2] == repo_ctx


async def test_origin_drift_offers_update_without_changing_checkout_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Default updates stay pending until the administrator accepts the offer."""
    deps = _UpdateDeps(offer_update=AsyncMock(), trigger_deploy=AsyncMock())
    state = sync_poll.HostSyncState(
        last_origin_sha="old-origin",
        deployed_sha="old-deploy",
        config_hash="config",
        local_head="old-deploy",
    )
    update_main = AsyncMock(return_value=True)
    monkeypatch.setattr(sync_poll, "host_get_origin_main_sha", lambda _root: "new-origin")
    monkeypatch.setattr(sync_poll, "host_update_main", update_main)

    changed = await sync_poll.check_origin_drift(
        tmp_path,
        state,
        None,
        deps,
        auto_deploy=False,
    )

    assert not changed
    update_main.assert_not_awaited()
    deps.offer_update.assert_awaited_once_with("new-origin")
    deps.trigger_deploy.assert_not_awaited()
    assert state.last_origin_sha == "new-origin"
    assert state.offered_sha == "new-origin"


async def test_origin_drift_retries_when_the_update_notification_cannot_send(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """A delivery failure keeps the remote revision eligible for the next poll."""
    deps = _UpdateDeps(offer_update=AsyncMock(return_value=False), trigger_deploy=AsyncMock())
    state = sync_poll.HostSyncState(
        last_origin_sha="old-origin",
        deployed_sha="old-deploy",
        config_hash="config",
        local_head="old-deploy",
    )
    monkeypatch.setattr(sync_poll, "host_get_origin_main_sha", lambda _root: "new-origin")

    changed = await sync_poll.check_origin_drift(
        tmp_path,
        state,
        None,
        deps,
        auto_deploy=False,
    )

    assert not changed
    deps.offer_update.assert_awaited_once_with("new-origin")
    assert state.last_origin_sha == "old-origin"
    assert not state.offered_sha


async def test_origin_drift_keeps_existing_auto_deploy_behavior_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """An explicit opt-in still fetches and deploys immediately."""
    deps = _UpdateDeps(offer_update=AsyncMock(), trigger_deploy=AsyncMock())
    state = sync_poll.HostSyncState(
        last_origin_sha="old-origin",
        deployed_sha="old-deploy",
        config_hash="config",
        local_head="old-deploy",
    )
    monkeypatch.setattr(sync_poll, "host_get_origin_main_sha", lambda _root: "new-origin")
    monkeypatch.setattr(sync_poll, "host_update_main", lambda _root: True)
    monkeypatch.setattr(sync_poll, "get_local_head_sha", lambda _root: "new-deploy")
    monkeypatch.setattr(sync_poll, "needs_deploy", lambda _old, _new: True)
    monkeypatch.setattr(sync_poll, "needs_container_rebuild", lambda _old, _new: True)

    changed = await sync_poll.check_origin_drift(
        tmp_path,
        state,
        None,
        deps,
        auto_deploy=True,
    )

    assert changed
    deps.offer_update.assert_not_awaited()
    deps.trigger_deploy.assert_awaited_once_with("old-deploy", rebuild=True)
