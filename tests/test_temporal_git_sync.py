"""Tests for the Temporal host-repository synchronization activity."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest
from conftest import make_settings

import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler
from pynchy.config.models import NotificationsConfig
from pynchy.host.orchestrator.temporal import git_sync
from pynchy.state import (
    claim_deployment,
    complete_deployment,
    init_test_database,
    initialize_deployment_state,
    set_router_state,
)
from pynchy.types import DeployRevision, WorkspaceProfile


@dataclass
class _RuntimeDeps:
    """The scheduler-dependency subset used by the git-sync Temporal adapter."""

    workspaces: dict[str, WorkspaceProfile]
    broadcast_host_message: object


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
        "_record_activity_result",
        lambda task_id, result: recorded.append((task_id, result)),
    )

    assert await git_sync.run_host_git_sync() == "skipped"
    assert not called
    assert recorded == [(git_sync.HOST_GIT_SYNC_ID, "skipped")]


async def test_trigger_deploy_reports_workflow_start_failure_after_rolling_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """A Temporal outage is a failed auto-deploy, not a silent changed checkout."""
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
    adapter = git_sync._TemporalGitSyncDeps(runtime_deps, reason="host_git_sync")
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
        await adapter.trigger_deploy("old-sha")

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
        git_sync._HOST_STATE_KEY,
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
    monkeypatch.setattr(git_sync, "_check_origin_drift", AsyncMock(return_value=False))
    runtime_deps = object()
    monkeypatch.setattr(git_sync, "_require_scheduler_deps", lambda: runtime_deps)
    trigger_deploy = AsyncMock()
    monkeypatch.setattr(git_sync._TemporalGitSyncDeps, "trigger_deploy", trigger_deploy)
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        git_sync,
        "_record_activity_result",
        lambda task_id, result: recorded.append((task_id, result)),
    )

    assert await git_sync.run_host_git_sync() == "idle"
    trigger_deploy.assert_not_awaited()
    assert recorded == [(git_sync.HOST_GIT_SYNC_ID, "idle")]
