"""Public git-sync behavior when failures have no diagnostic text."""

from __future__ import annotations

import subprocess  # noqa: S404 - tests mock fixed git commands.
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.host.git_ops.api import (
    RepoContext,
    check_local_head_drift,
    find_pynchy_repo_ctx,
    host_update_main_result,
    sync_poll,
)

if TYPE_CHECKING:
    from pathlib import Path


def _result(
    args: object, returncode: int = 0, *, stdout: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr="")


class _Deps:
    def __init__(self) -> None:
        self.wake_worktree_conflict = AsyncMock()
        self.offer_update = None
        self.trigger_deploy = AsyncMock()

    async def broadcast_host_message(self, _jid: str, _text: str) -> None:
        return None

    async def broadcast_system_notice(self, _jid: str, _text: str) -> None:
        return None

    def has_active_session(self, _group_folder: str) -> bool:
        return False

    def workspaces(self) -> dict[str, object]:
        return {}


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("fetch", "git fetch origin failed with exit 1"),
        ("rebase", "git rebase origin/main failed with exit 1"),
        ("stash-pop", "git stash pop failed with exit 1"),
    ],
)
def test_update_main_reports_bare_failures_without_trailing_diagnostics(
    tmp_path: Path, failure: str, expected: str
) -> None:
    """Failure summaries remain stable when Git emits no stderr."""
    (tmp_path / ".git").mkdir()

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        command = args[0] if args else kwargs["args"]
        assert isinstance(command, tuple | list)
        command = command[1:]
        if command[:2] == ["status", "--porcelain"]:
            return _result(command, stdout="M dirty.txt\n" if failure == "stash-pop" else "")
        if failure == "fetch" and command[:1] == ["fetch"]:
            return _result(command, 1)
        if failure == "rebase" and command[:1] == ["rebase"] and "--abort" not in command:
            return _result(command, 1)
        if failure == "stash-pop" and command[:2] == ["stash", "pop"]:
            return _result(command, 1)
        return _result(command)

    with (
        patch("pynchy.host.git_ops.utils._run_git_process", side_effect=fake_run),
        patch("pynchy.host.git_ops.sync_poll.detect_main_branch", return_value="main"),
        patch("pynchy.host.git_ops.sync_poll.push_local_commits"),
    ):
        result = host_update_main_result(tmp_path)

    assert result.succeeded is False
    assert result.error == expected


@pytest.mark.asyncio
async def test_local_drift_suppresses_duplicate_worktree_notification(tmp_path: Path) -> None:
    """Automatic local-drift handling notifies once for an inspected revision."""
    state = sync_poll.HostSyncState(
        last_origin_sha="origin",
        deployed_sha="old-head",
        config_hash="config",
    )
    repo = RepoContext("owner/project", tmp_path, tmp_path / "worktrees")
    deps = _Deps()
    notify = AsyncMock()
    sync_poll.last_notified_sha[str(tmp_path)] = "local-head"

    with (
        patch("pynchy.host.git_ops.sync_poll.get_local_head_sha", return_value="local-head"),
        patch("pynchy.host.git_ops.sync_poll.needs_deploy", return_value=True),
        patch("pynchy.host.git_ops.sync_poll.needs_container_rebuild", return_value=False),
        patch("pynchy.host.git_ops.sync_poll.host_notify_worktree_updates", notify),
    ):
        stopped = await check_local_head_drift(tmp_path, state, repo, deps, auto_deploy=True)

    assert stopped is True
    notify.assert_not_awaited()
    deps.trigger_deploy.assert_awaited_once_with("old-head", rebuild=False)


@pytest.mark.asyncio
async def test_local_drift_deploys_without_a_pynchy_repo_context(tmp_path: Path) -> None:
    """Local deployment does not require worktree notification ownership."""
    state = sync_poll.HostSyncState(
        last_origin_sha="origin",
        deployed_sha="old-head",
        config_hash="config",
    )
    deps = _Deps()

    with (
        patch("pynchy.host.git_ops.sync_poll.get_local_head_sha", return_value="local-head"),
        patch("pynchy.host.git_ops.sync_poll.needs_deploy", return_value=True),
        patch("pynchy.host.git_ops.sync_poll.needs_container_rebuild", return_value=True),
    ):
        stopped = await check_local_head_drift(tmp_path, state, None, deps, auto_deploy=True)

    assert stopped is True
    deps.trigger_deploy.assert_awaited_once_with("old-head", rebuild=True)


@pytest.mark.asyncio
async def test_repeated_manual_local_drift_offer_is_suppressed(tmp_path: Path) -> None:
    """An already-offered local revision is not offered to the admin twice."""
    state = sync_poll.HostSyncState(
        last_origin_sha="origin",
        deployed_sha="old-head",
        config_hash="config",
        offered_sha="local-head",
    )
    offer = AsyncMock()
    deps = _Deps()
    deps.offer_update = offer

    with (
        patch("pynchy.host.git_ops.sync_poll.get_local_head_sha", return_value="local-head"),
        patch("pynchy.host.git_ops.sync_poll.needs_deploy", return_value=True),
    ):
        stopped = await check_local_head_drift(tmp_path, state, None, deps, auto_deploy=False)

    assert stopped is False
    offer.assert_not_awaited()


def test_find_pynchy_repo_context_selects_matching_configured_repo(tmp_path: Path) -> None:
    """The host repository is selected by resolved root, not list position."""
    target = RepoContext("owner/pynchy", tmp_path, tmp_path / "worktrees")
    other = RepoContext("owner/other", tmp_path / "other", tmp_path / "other-worktrees")

    def resolve(slug: str) -> RepoContext:
        return {"owner/other": other, "owner/pynchy": target}[slug]

    with patch("pynchy.host.git_ops.sync_poll.get_repo_context", side_effect=resolve):
        selected = find_pynchy_repo_ctx(("owner/other", "owner/pynchy"), tmp_path)

    assert selected == target
