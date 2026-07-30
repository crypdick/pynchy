"""Shared fixtures and helpers for Git publication tests."""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - test helpers mock subprocess behavior and exceptions
from contextlib import ExitStack
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from conftest import NullIpcDeps, init_test_database, make_settings

from pynchy.host.git_ops.api import RepoContext
from pynchy.workspace.api import WorkspaceProfile

if TYPE_CHECKING:
    from pathlib import Path


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run fixed Git arguments against a temporary repository."""
    return subprocess.run(  # noqa: S603 - test helper runs fixed git argv against temp repos
        ["git", *args],  # noqa: S607 - test helper deliberately resolves git from PATH
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def no_pr_result(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Model a successful `gh pr list` with no matching branch."""
    return subprocess.CompletedProcess(args=args, returncode=0, stdout="[]", stderr="")


def managed_pr_result(
    args: list[str],
    *,
    base_sha: str,
    head_sha: str,
    branch_name: str | None = None,
    head_ref_name: str | None = None,
    cross_repository: bool = False,
    state: str = "OPEN",
) -> subprocess.CompletedProcess[str]:
    """Build one `gh pr list` response for a managed branch."""
    return subprocess.CompletedProcess(
        args=args,
        returncode=0,
        stdout=json.dumps(
            [
                {
                    "url": "https://github.com/owner/repo/pull/1",
                    "baseRefName": "main",
                    "baseRefOid": base_sha,
                    "headRefName": (
                        head_ref_name
                        if head_ref_name is not None
                        else branch_name
                        if branch_name is not None
                        else args[args.index("--head") + 1]
                    ),
                    "headRefOid": head_sha,
                    "isCrossRepository": cross_repository,
                    "state": state,
                }
            ]
        ),
        stderr="",
    )


def make_bare_origin(tmp_path: Path, *, object_format: str | None = None) -> Path:
    """Create a bare origin repository with one commit on main."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    init_args = ["init", "--bare", "--initial-branch=main"]
    if object_format is not None:
        init_args.append(f"--object-format={object_format}")
    git(origin, *init_args)

    clone = tmp_path / "setup-clone"
    git(tmp_path, "clone", str(origin), str(clone))
    git(clone, "config", "user.email", "test@test.com")
    git(clone, "config", "user.name", "Test")
    (clone / "README.md").write_text("initial")
    git(clone, "add", "README.md")
    git(clone, "commit", "-m", "initial commit")
    git(clone, "push", "origin", "main")
    return origin


def make_project(tmp_path: Path, origin: Path) -> Path:
    """Clone origin into a project directory."""
    project = tmp_path / "project"
    git(tmp_path, "clone", str(origin), str(project))
    git(project, "config", "user.email", "test@test.com")
    git(project, "config", "user.name", "Test")
    return project


@pytest.fixture
def git_env(tmp_path: Path):
    """Set up origin and project repositories with patched settings."""
    origin = make_bare_origin(tmp_path)
    project = make_project(tmp_path, origin)
    worktrees_dir = tmp_path / "worktrees"

    settings = make_settings(project_root=project, worktrees_dir=worktrees_dir)
    repo_ctx = RepoContext(slug="owner/repo", root=project, worktrees_dir=worktrees_dir)

    with ExitStack() as stack:
        stack.enter_context(patch("pynchy.host.git_ops.utils._default_cwd", settings.project_root))
        stack.enter_context(
            patch(
                "pynchy.host.git_ops.managed_feature.managed_feature_remote_url",
                return_value=str(origin),
            )
        )
        stack.enter_context(
            patch("pynchy.host.git_ops.managed_feature.git_env_with_token", return_value=None)
        )
        stack.enter_context(patch("pynchy.host.git_ops.sync.git_env_with_token", return_value=None))
        stack.enter_context(
            patch("pynchy.host.git_ops.worktree.git_env_with_token", return_value=None)
        )
        stack.enter_context(
            patch("pynchy.host.git_ops.worktree_sync.git_env_with_token", return_value=None)
        )
        yield {
            "origin": origin,
            "project": project,
            "worktrees_dir": worktrees_dir,
            "repo_ctx": repo_ctx,
            "settings": settings,
        }


def managed_record(slug: str, **overrides: str) -> dict[str, str]:
    """Build one version-2 manifest record for a temporary managed feature."""
    record = {
        "key": slug.replace("-", "_"),
        "name": slug,
        "slug": slug,
        "branch": slug,
        "worktree": f".worktrees/{slug}",
        "target_branch": "main",
        "status": "active",
    }
    record.update(overrides)
    return record


def write_managed_manifest(
    project: Path,
    records: list[dict[str, str]],
    *,
    version: int = 2,
) -> None:
    """Write only manifest fields trusted by the host resolver."""
    lines = [f"version = {version}", ""]
    for record in records:
        lines.extend(
            [
                f"[features.{record['key']}]",
                f'name = "{record["name"]}"',
                f'slug = "{record["slug"]}"',
                f'branch = "{record["branch"]}"',
                f'worktree = "{record["worktree"]}"',
                f'target_branch = "{record["target_branch"]}"',
                f'status = "{record["status"]}"',
                "",
            ]
        )
    manifest = project / ".new-feature" / "manifest.toml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("\n".join(lines), encoding="utf-8")


def create_managed_feature(git_env: dict, slug: str, *, branch: str | None = None) -> Path:
    """Create a committed feature worktree at its manifest-owned location."""
    project = git_env["project"]
    worktree = project / ".worktrees" / slug
    git(project, "worktree", "add", "-b", branch or slug, str(worktree), "main")
    (worktree / f"{slug}.txt").write_text(f"{slug} change\n", encoding="utf-8")
    git(worktree, "add", f"{slug}.txt")
    git(worktree, "commit", "-m", f"add {slug}")
    return worktree


def replace_head_with_signed_commit(worktree: Path) -> str:
    """Replace fixture head with a commit Git treats as PGP-signed."""
    tree = git(worktree, "rev-parse", "HEAD^{tree}").stdout.strip()
    parent = git(worktree, "rev-parse", "HEAD^").stdout.strip()
    commit = (
        f"tree {tree}\n"
        f"parent {parent}\n"
        "author Test <test@test.com> 0 +0000\n"
        "committer Test <test@test.com> 0 +0000\n"
        "gpgsig -----BEGIN PGP SIGNATURE-----\n"
        " fake signature\n"
        " -----END PGP SIGNATURE-----\n"
        "\n"
        "signed feature\n"
    )
    result = subprocess.run(
        ["git", "hash-object", "-t", "commit", "-w", "--stdin"],  # noqa: S607 - test deliberately resolves git from PATH.
        cwd=str(worktree),
        input=commit,
        capture_output=True,
        text=True,
        check=True,
    )
    signed_head = result.stdout.strip()
    git(worktree, "reset", "--hard", signed_head)
    return signed_head


class GitPolicyDeps(NullIpcDeps):
    """IPC dependencies shared by Git publication tests."""

    def __init__(self, groups: dict[str, WorkspaceProfile]):
        self._groups = groups
        self.host_messages: list[tuple[str, str]] = []
        self.system_notices: list[tuple[str, str]] = []
        self.deploy_calls: list[tuple[str, bool]] = []
        self.cleared_sessions: list[str] = []
        self.cleared_chats: list[str] = []
        self.enqueued_checks: list[str] = []

    async def broadcast_host_message(self, jid: str, text: str) -> None:
        self.host_messages.append((jid, text))

    async def broadcast_system_notice(self, jid: str, text: str) -> None:
        self.system_notices.append((jid, text))

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return self._groups

    async def clear_session(self, group_folder: str) -> None:
        self.cleared_sessions.append(group_folder)

    async def clear_chat_history(self, chat_jid: str) -> None:
        self.cleared_chats.append(chat_jid)

    def enqueue_message_check(self, group_jid: str) -> None:
        self.enqueued_checks.append(group_jid)

    async def trigger_deploy(self, previous_sha: str, *, rebuild: bool = True) -> None:
        self.deploy_calls.append((previous_sha, rebuild))


@pytest.fixture
async def git_policy_deps() -> GitPolicyDeps:
    """Provide isolated IPC dependencies for Git publication tests."""
    await init_test_database()
    return GitPolicyDeps(
        {
            "agent@g.us": WorkspaceProfile(
                jid="agent@g.us",
                name="Agent",
                folder="agent-1",
                trigger="@test",
                added_at="2024-01-01",
            ),
        }
    )
