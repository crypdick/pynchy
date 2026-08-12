"""Public git-sync probe and drift edge behavior."""

from __future__ import annotations

import subprocess  # noqa: S404 - tests mock fixed git commands.
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.host.git_ops.api import RepoContext, check_local_head_drift, sync_poll

if TYPE_CHECKING:
    from pathlib import Path

    from pynchy.workspace.api import WorkspaceProfile


class _Deps:
    def __init__(self, offer_update: AsyncMock | None = None) -> None:
        self.offer_update = offer_update
        self.trigger_deploy = AsyncMock()

    async def broadcast_host_message(self, _jid: str, _text: str) -> None:
        return None

    async def broadcast_system_notice(self, _jid: str, _text: str) -> None:
        return None

    async def wake_worktree_conflict(self, _jid: str) -> None:
        return None

    def has_active_session(self, _group_folder: str) -> bool:
        return False

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return {}


def _ok(*, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


async def test_local_head_without_deployable_changes_advances_baseline(
    monkeypatch,
    tmp_path: Path,
):
    state = sync_poll.HostSyncState(
        last_origin_sha="origin",
        deployed_sha="old-head",
        config_hash="config",
    )
    monkeypatch.setattr(sync_poll, "get_local_head_sha", lambda _root: "local-head")
    monkeypatch.setattr(sync_poll, "needs_deploy", lambda _old, _new: False)

    assert await check_local_head_drift(tmp_path, state, None, _Deps(), auto_deploy=True) is False
    assert state.deployed_sha == "local-head"


async def test_manual_local_head_drift_offers_update(
    monkeypatch,
    tmp_path: Path,
):
    state = sync_poll.HostSyncState(
        last_origin_sha="origin",
        deployed_sha="old-head",
        config_hash="config",
    )
    offer = AsyncMock(return_value=True)
    monkeypatch.setattr(sync_poll, "get_local_head_sha", lambda _root: "local-head")
    monkeypatch.setattr(sync_poll, "needs_deploy", lambda _old, _new: True)

    assert (
        await check_local_head_drift(tmp_path, state, None, _Deps(offer), auto_deploy=False)
        is False
    )
    offer.assert_awaited_once_with("local-head")
    assert state.offered_sha == "local-head"


async def test_local_head_offer_exception_is_retryable(
    monkeypatch,
    tmp_path: Path,
):
    state = sync_poll.HostSyncState(
        last_origin_sha="origin",
        deployed_sha="old-head",
        config_hash="config",
    )
    offer = AsyncMock(side_effect=RuntimeError("notification failed"))
    monkeypatch.setattr(sync_poll, "get_local_head_sha", lambda _root: "local-head")
    monkeypatch.setattr(sync_poll, "needs_deploy", lambda _old, _new: True)

    assert (
        await check_local_head_drift(tmp_path, state, None, _Deps(offer), auto_deploy=False)
        is False
    )
    assert not state.offered_sha


async def test_auto_local_head_drift_notifies_and_deploys(
    monkeypatch,
    tmp_path: Path,
):
    state = sync_poll.HostSyncState(
        last_origin_sha="origin",
        deployed_sha="old-head",
        config_hash="config",
    )
    repo = RepoContext("owner/project", tmp_path, tmp_path / "worktrees")
    notify = AsyncMock()
    deps = _Deps()
    monkeypatch.setattr(sync_poll, "get_local_head_sha", lambda _root: "local-head")
    monkeypatch.setattr(sync_poll, "needs_deploy", lambda _old, _new: True)
    monkeypatch.setattr(sync_poll, "needs_container_rebuild", lambda _old, _new: True)
    monkeypatch.setattr(sync_poll, "host_notify_worktree_updates", notify)
    sync_poll.last_notified_sha.pop(str(tmp_path), None)

    assert await check_local_head_drift(tmp_path, state, repo, deps, auto_deploy=True) is True
    notify.assert_awaited_once()
    deps.trigger_deploy.assert_awaited_once_with("old-head", rebuild=True)


def test_probe_timeout_returns_redacted_failure(tmp_path: Path):
    with (
        patch("pynchy.host.git_ops.sync_poll.detect_main_branch", return_value="main"),
        patch(
            "pynchy.host.git_ops.utils._run_git_process",
            side_effect=subprocess.TimeoutExpired("git", 30),
        ),
    ):
        probe = sync_poll.probe_origin_main_sha(tmp_path, {"GH_TOKEN": "secret"})

    assert probe.sha is None
    assert probe.error is not None
    assert "timed out" in probe.error.lower()


def test_probe_success_without_revision_reports_specific_failure(tmp_path: Path):
    with (
        patch("pynchy.host.git_ops.sync_poll.detect_main_branch", return_value="main"),
        patch("pynchy.host.git_ops.utils._run_git_process", return_value=_ok()),
    ):
        probe = sync_poll.probe_origin_main_sha(tmp_path)

    assert probe.error == ("git ls-remote origin refs/heads/main returned no revision")


def test_deploy_config_hash_requires_composed_runtime(monkeypatch):
    monkeypatch.setattr(sync_poll, "_git_sync_runtime", None)

    with pytest.raises(RuntimeError, match="git sync runtime has not been configured"):
        sync_poll.get_deploy_config_hash()


async def test_duplicate_origin_notification_is_suppressed(
    monkeypatch,
    tmp_path: Path,
):
    state = sync_poll.HostSyncState(
        last_origin_sha="old-origin",
        deployed_sha="old-head",
        config_hash="config",
        local_head="old-head",
    )
    repo = RepoContext("owner/project", tmp_path, tmp_path / "worktrees")
    deps = _Deps()
    notify = AsyncMock()
    monkeypatch.setattr(sync_poll, "host_get_origin_main_sha", lambda _root: "new-origin")
    monkeypatch.setattr(sync_poll, "host_update_main", lambda _root: True)
    monkeypatch.setattr(sync_poll, "get_local_head_sha", lambda _root: "new-head")
    monkeypatch.setattr(sync_poll, "needs_deploy", lambda _old, _new: False)
    monkeypatch.setattr(sync_poll, "host_notify_worktree_updates", notify)
    sync_poll.last_notified_sha[str(tmp_path)] = "new-head"

    assert (
        await sync_poll.check_origin_drift(tmp_path, state, repo, deps, auto_deploy=True) is False
    )
    notify.assert_not_awaited()
