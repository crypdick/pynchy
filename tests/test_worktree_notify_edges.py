"""Worktree notification lifecycle boundary contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from pynchy.host.git_ops.api import RepoContext, build_rebase_notice, host_notify_worktree_updates

if TYPE_CHECKING:
    from pathlib import Path


class _NotifyDeps:
    async def broadcast_host_message(self, _jid: str, _text: str) -> None:
        pass

    async def broadcast_system_notice(self, _jid: str, _text: str) -> None:
        pass

    async def wake_worktree_conflict(self, _jid: str) -> None:
        pass

    def has_active_session(self, _group_folder: str) -> bool:
        return False

    def workspaces(self) -> dict[str, object]:
        return {}


async def test_worktree_notifications_skip_missing_worktree_root(tmp_path: Path) -> None:
    repo_ctx = RepoContext("owner/repo", tmp_path, tmp_path / "missing-worktrees")

    await host_notify_worktree_updates(None, _NotifyDeps(), repo_ctx)


async def test_worktree_notifications_skip_non_directory_entries(
    monkeypatch, tmp_path: Path
) -> None:
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    (worktrees_dir / "not-a-worktree").write_text("ignored")
    monkeypatch.setattr(
        "pynchy.host.git_ops._worktree_notify.detect_main_branch", lambda cwd: "main"
    )
    repo_ctx = RepoContext("owner/repo", tmp_path, worktrees_dir)

    await host_notify_worktree_updates(None, _NotifyDeps(), repo_ctx)


def test_rebase_notice_omits_commit_message_when_log_lookup_fails(tmp_path: Path) -> None:
    with patch(
        "pynchy.host.git_ops._worktree_notify.run_git",
        side_effect=(
            Mock(returncode=0, stdout="2 files changed\n"),
            Mock(returncode=1, stdout=""),
        ),
    ):
        notice = build_rebase_notice(tmp_path, "old-head", 1)

    assert "Auto-rebased 1 commit(s)" in notice
    assert "2 files changed" in notice
    assert "Commit:" not in notice
