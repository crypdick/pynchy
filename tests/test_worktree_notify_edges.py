"""Worktree notification lifecycle boundary contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pynchy.host.git_ops.api import RepoContext, host_notify_worktree_updates

if TYPE_CHECKING:
    from pathlib import Path


class _NotifyDeps:
    async def broadcast_host_message(self, _jid: str, _text: str) -> None:
        pass

    async def broadcast_system_notice(self, _jid: str, _text: str) -> None:
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
