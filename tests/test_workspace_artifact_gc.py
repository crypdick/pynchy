"""Archived workspace artifacts leave safely through one public cleanup seam."""

from __future__ import annotations

import subprocess  # noqa: S404 - tests build isolated temporary Git repositories.
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from conftest import NullChannel, make_settings

from pynchy.config.api import ProfileConfig, WorkspaceConfig
from pynchy.conversation.api import dynamic_thread_folder
from pynchy.conversation.models import ConversationId
from pynchy.conversation.workspaces import routed_conversation_folder
from pynchy.host.orchestrator.api import (
    cleanup_orphaned_workspace_artifacts,
    cleanup_workspace_artifacts,
)
from pynchy.host.orchestrator.workspace_config import reconcile_workspaces
from pynchy.workspace.api import WorkspaceProfile


class _PresenceChannel(NullChannel):
    name = "discord"

    def __init__(self, presence: dict[str, bool]) -> None:
        self._presence = presence

    def owns_jid(self, jid: str) -> bool:
        return jid.startswith("discord:")

    async def conversation_exists(self, jid: str) -> bool:
        return self._presence[jid]


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed Git argv targets only the pytest temp directory.
        ["git", *args],  # noqa: S607 - test resolves the host Git executable.
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _run_git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed Git argv targets only the pytest temp directory.
        ["git", *args],  # noqa: S607 - test resolves the host Git executable.
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def _git_result(
    args: tuple[str, ...],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def _managed_worktree(tmp_path: Path, folder: str) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text(".venv/\n")
    (repo / "README.md").write_text("test\n")
    _git(repo, "add", ".gitignore", "README.md")
    _git(repo, "commit", "-m", "initial")

    worktree = tmp_path / "worktrees" / "owner" / "repo" / folder
    worktree.parent.mkdir(parents=True)
    _git(repo, "worktree", "add", "-b", f"worktree/{folder}", str(worktree))
    return repo, worktree


def _artifact_roots(tmp_path: Path, folder: str) -> tuple[Path, Path, Path]:
    data_dir = tmp_path / "data"
    groups_dir = tmp_path / "groups"
    group_logs = groups_dir / folder / "logs"
    group_logs.mkdir(parents=True)
    (group_logs / "runtime.log").write_text("stale")
    for base in (data_dir / name for name in ("sessions", "ipc", "env", "approvals")):
        path = base / folder
        path.mkdir(parents=True)
        (path / "artifact").write_text("stale")
    return data_dir, groups_dir, tmp_path / "worktrees"


def test_cleanup_removes_clean_worktree_and_ephemeral_runtime_dirs(tmp_path: Path) -> None:
    folder = "project__thread_discord-channel-123"
    repo, worktree = _managed_worktree(tmp_path, folder)
    (worktree / ".venv").mkdir()
    (worktree / ".venv" / "ignored").write_text("cache")
    data_dir, groups_dir, worktrees_dir = _artifact_roots(tmp_path, folder)

    assert cleanup_workspace_artifacts(
        folder,
        data_dir=data_dir,
        groups_dir=groups_dir,
        worktrees_dir=worktrees_dir,
        git=_run_git,
    )

    assert not worktree.exists()
    assert _git(repo, "show-ref", "--verify", f"refs/heads/worktree/{folder}").returncode == 0
    assert not (groups_dir / folder).exists()
    for name in ("sessions", "ipc", "env", "approvals"):
        assert not (data_dir / name / folder).exists()


def test_cleanup_retains_everything_when_worktree_has_uncommitted_work(tmp_path: Path) -> None:
    folder = "project__thread_discord-channel-456"
    _repo, worktree = _managed_worktree(tmp_path, folder)
    (worktree / "valuable.txt").write_text("unfinished")
    data_dir, groups_dir, worktrees_dir = _artifact_roots(tmp_path, folder)

    assert not cleanup_workspace_artifacts(
        folder,
        data_dir=data_dir,
        groups_dir=groups_dir,
        worktrees_dir=worktrees_dir,
        git=_run_git,
    )

    assert worktree.is_dir()
    assert (groups_dir / folder / "logs" / "runtime.log").is_file()
    assert (data_dir / "sessions" / folder / "artifact").is_file()


def test_cleanup_retains_agent_workspace_files(tmp_path: Path) -> None:
    folder = "project__thread_discord-channel-789"
    data_dir, groups_dir, worktrees_dir = _artifact_roots(tmp_path, folder)
    (groups_dir / folder / "notebook.ipynb").write_text("valuable")

    assert not cleanup_workspace_artifacts(
        folder,
        data_dir=data_dir,
        groups_dir=groups_dir,
        worktrees_dir=worktrees_dir,
        git=_run_git,
    )

    assert (groups_dir / folder / "notebook.ipynb").is_file()
    assert (data_dir / "sessions" / folder / "artifact").is_file()


def test_cleanup_retains_artifacts_when_worktree_root_is_unsafe(tmp_path: Path) -> None:
    folder = "project__thread_discord-channel-987"
    data_dir, groups_dir, worktrees_dir = _artifact_roots(tmp_path, folder)
    worktrees_dir.symlink_to(tmp_path / "elsewhere", target_is_directory=True)

    assert not cleanup_workspace_artifacts(
        folder,
        data_dir=data_dir,
        groups_dir=groups_dir,
        worktrees_dir=worktrees_dir,
        git=_run_git,
    )

    assert (groups_dir / folder / "logs" / "runtime.log").is_file()
    assert (data_dir / "sessions" / folder / "artifact").is_file()


def test_cleanup_rejects_invalid_folder_and_unsafe_artifact(tmp_path: Path) -> None:
    assert not cleanup_workspace_artifacts(
        "../escape",
        data_dir=tmp_path / "data",
        groups_dir=tmp_path / "groups",
        worktrees_dir=tmp_path / "worktrees",
        git=_run_git,
    )

    folder = "project__thread_discord-channel-unsafe"
    groups_dir = tmp_path / "groups"
    groups_dir.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    (groups_dir / folder).symlink_to(target, target_is_directory=True)

    assert not cleanup_workspace_artifacts(
        folder,
        data_dir=tmp_path / "data",
        groups_dir=groups_dir,
        worktrees_dir=tmp_path / "worktrees",
        git=_run_git,
    )


def test_cleanup_retains_artifacts_when_worktree_root_is_a_file(tmp_path: Path) -> None:
    folder = "project__thread_discord-channel-root-file"
    data_dir, groups_dir, worktrees_dir = _artifact_roots(tmp_path, folder)
    worktrees_dir.write_text("unsafe")

    assert not cleanup_workspace_artifacts(
        folder,
        data_dir=data_dir,
        groups_dir=groups_dir,
        worktrees_dir=worktrees_dir,
        git=_run_git,
    )


def test_cleanup_retains_artifacts_when_worktree_scan_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = "project__thread_discord-channel-scan-failure"
    data_dir, groups_dir, worktrees_dir = _artifact_roots(tmp_path, folder)
    worktrees_dir.mkdir()
    real_iterdir = Path.iterdir

    def iterdir(path: Path):
        if path == worktrees_dir:
            raise OSError("scan failed")
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)

    assert not cleanup_workspace_artifacts(
        folder,
        data_dir=data_dir,
        groups_dir=groups_dir,
        worktrees_dir=worktrees_dir,
        git=_run_git,
    )


def test_cleanup_retains_artifacts_when_group_scan_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = "project__thread_discord-channel-group-scan"
    data_dir, groups_dir, worktrees_dir = _artifact_roots(tmp_path, folder)
    group_dir = groups_dir / folder
    real_iterdir = Path.iterdir

    def iterdir(path: Path):
        if path == group_dir:
            raise OSError("scan failed")
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)

    assert not cleanup_workspace_artifacts(
        folder,
        data_dir=data_dir,
        groups_dir=groups_dir,
        worktrees_dir=worktrees_dir,
        git=_run_git,
    )


def test_cleanup_retains_unsafe_nested_worktree_path(tmp_path: Path) -> None:
    folder = "project__thread_discord-channel-worktree-link"
    data_dir, groups_dir, worktrees_dir = _artifact_roots(tmp_path, folder)
    repo_dir = worktrees_dir / "owner" / "repo"
    repo_dir.mkdir(parents=True)
    target = tmp_path / "outside-worktree"
    target.mkdir()
    (repo_dir / folder).symlink_to(target, target_is_directory=True)

    assert not cleanup_workspace_artifacts(
        folder,
        data_dir=data_dir,
        groups_dir=groups_dir,
        worktrees_dir=worktrees_dir,
        git=_run_git,
    )


@pytest.mark.parametrize("failure", ["metadata-read", "metadata-shape", "remove"])
def test_cleanup_retains_artifacts_when_git_cannot_safely_remove_worktree(
    tmp_path: Path,
    failure: str,
) -> None:
    folder = f"project__thread_discord-channel-{failure}"
    data_dir, groups_dir, worktrees_dir = _artifact_roots(tmp_path, folder)
    worktree = worktrees_dir / "owner" / "repo" / folder
    worktree.mkdir(parents=True)
    common_dir = tmp_path / ("metadata" if failure == "metadata-shape" else "repo-root/.git")
    common_dir.mkdir(parents=True)

    def git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        if args[0] == "status":
            return _git_result(args)
        if args[0] == "rev-parse":
            return _git_result(
                args,
                returncode=int(failure == "metadata-read"),
                stdout=f"{common_dir}\n",
            )
        return _git_result(args, returncode=1, stderr="remove failed")

    assert not cleanup_workspace_artifacts(
        folder,
        data_dir=data_dir,
        groups_dir=groups_dir,
        worktrees_dir=worktrees_dir,
        git=git,
    )
    assert worktree.is_dir()
    assert (groups_dir / folder).is_dir()


def test_cleanup_retains_artifacts_when_removal_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = "project__thread_discord-channel-remove-failure"
    data_dir, groups_dir, worktrees_dir = _artifact_roots(tmp_path, folder)

    def fail_removal(_path: Path) -> None:
        raise OSError("remove failed")

    monkeypatch.setattr(
        "pynchy.host.orchestrator.workspace_artifacts.shutil.rmtree",
        fail_removal,
    )

    assert not cleanup_workspace_artifacts(
        folder,
        data_dir=data_dir,
        groups_dir=groups_dir,
        worktrees_dir=worktrees_dir,
        git=_run_git,
    )
    assert (groups_dir / folder).is_dir()


def test_orphan_sweep_tolerates_unreadable_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    groups_dir = tmp_path / "groups"
    worktrees_dir = tmp_path / "worktrees"
    (data_dir / "sessions").mkdir(parents=True)
    groups_dir.mkdir()
    worktrees_dir.mkdir()
    real_iterdir = Path.iterdir

    def iterdir(path: Path):
        if path in {groups_dir, worktrees_dir}:
            raise OSError("scan failed")
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)

    assert (
        cleanup_orphaned_workspace_artifacts(
            set(),
            data_dir=data_dir,
            groups_dir=groups_dir,
            worktrees_dir=worktrees_dir,
            git=_run_git,
        )
        == []
    )


def test_orphan_sweep_removes_worktree_only_artifact(tmp_path: Path) -> None:
    folder = "project__thread_discord-channel-worktree-only"
    repo, worktree = _managed_worktree(tmp_path, folder)

    assert cleanup_orphaned_workspace_artifacts(
        set(),
        data_dir=tmp_path / "data",
        groups_dir=tmp_path / "groups",
        worktrees_dir=tmp_path / "worktrees",
        git=_run_git,
    ) == [folder]

    assert not worktree.exists()
    assert _git(repo, "show-ref", "--verify", f"refs/heads/worktree/{folder}").returncode == 0


def test_orphan_sweep_only_removes_unprotected_nonrouted_threads(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    groups_dir = tmp_path / "groups"
    worktrees_dir = tmp_path / "worktrees"
    orphan = "project__thread_discord-channel-123"
    protected = "project__thread_discord-channel-456"
    routed = "project__thread_conversation-conv_keep"
    static = "project"
    for folder in (orphan, protected, routed, static):
        (data_dir / "sessions" / folder).mkdir(parents=True)
        (groups_dir / folder).mkdir(parents=True)

    assert cleanup_orphaned_workspace_artifacts(
        {protected},
        data_dir=data_dir,
        groups_dir=groups_dir,
        worktrees_dir=worktrees_dir,
        git=_run_git,
    ) == [orphan]

    assert not (data_dir / "sessions" / orphan).exists()
    for folder in (protected, routed, static):
        assert (data_dir / "sessions" / folder).is_dir()


@pytest.mark.asyncio
async def test_registration_cleanup_preserves_owners_and_supports_optional_retirement() -> None:
    settings = make_settings(
        profiles={"support": ProfileConfig()},
        workspaces={"support": WorkspaceConfig(profiles=["support"])},
    )
    parent = WorkspaceProfile(
        jid="discord:channel:support",
        name="Support",
        folder="support",
        trigger="@pynchy",
    )

    def routed(suffix: str) -> WorkspaceProfile:
        return WorkspaceProfile(
            jid=f"discord:channel:{suffix}",
            name=f"Support/{suffix}",
            folder=routed_conversation_folder("support", ConversationId(f"conv_{suffix}")),
            trigger="@pynchy",
        )

    def provider_child(suffix: str) -> WorkspaceProfile:
        jid = f"discord:channel:{suffix}"
        return WorkspaceProfile(
            jid=jid,
            name=f"Support/{suffix}",
            folder=dynamic_thread_folder("support", jid),
            trigger="@pynchy",
        )

    routed_with_retire = routed("routed-with-retire")
    routed_without_retire = routed("routed-without-retire")
    provider_with_retire = provider_child("provider-with-retire")
    provider_without_retire = provider_child("provider-without-retire")
    session_owned = WorkspaceProfile(
        jid="discord:channel:session-owned",
        name="Session owned",
        folder="session-owned",
        trigger="@pynchy",
    )
    orphan = WorkspaceProfile(
        jid="discord:channel:orphan",
        name="Orphan",
        folder="orphan",
        trigger="@pynchy",
    )
    unregister = AsyncMock()
    retire = AsyncMock()

    with (
        patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=settings),
        patch(
            "pynchy.host.orchestrator.workspace_config.get_all_tasks",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "pynchy.host.orchestrator.workspace_artifacts.get_all_tasks",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "pynchy.host.orchestrator.workspace_artifacts.get_all_sessions",
            new_callable=AsyncMock,
            return_value={session_owned.folder: "session-1"},
        ),
        patch(
            "pynchy.host.orchestrator.workspace_registration.set_workspace_profile",
            new_callable=AsyncMock,
        ),
    ):
        await reconcile_workspaces(
            {
                profile.jid: profile
                for profile in (
                    parent,
                    routed_with_retire,
                    provider_with_retire,
                    session_owned,
                    orphan,
                )
            },
            [_PresenceChannel({provider_with_retire.jid: False})],
            AsyncMock(),
            unregister,
            retire_fn=retire,
        )
        await reconcile_workspaces(
            {
                profile.jid: profile
                for profile in (parent, routed_without_retire, provider_without_retire)
            },
            [_PresenceChannel({provider_without_retire.jid: False})],
            AsyncMock(),
            unregister,
        )

    assert [call.args[0] for call in retire.await_args_list] == [
        routed_with_retire.folder,
        provider_with_retire.folder,
        orphan.folder,
    ]
    assert [call.args[0] for call in unregister.await_args_list] == [
        routed_with_retire.jid,
        provider_with_retire.jid,
        orphan.jid,
        routed_without_retire.jid,
        provider_without_retire.jid,
    ]
