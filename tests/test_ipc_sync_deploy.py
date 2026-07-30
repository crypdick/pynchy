"""Tests for IPC sync_worktree_to_main and deploy edge cases.

These test the dispatch match branches for sync_worktree_to_main and deploy
that aren't covered by test_ipc_auth.py (which focuses on authorization) or
test_ipc_watcher.py (which focuses on the file scanning loop).

Key coverage gaps addressed:
- sync_worktree_to_main result file writing
- sync_worktree_to_main PR-only publication failures
- deploy fallback when chatJid is missing
- deploy with no admin group registered
"""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - tests construct CompletedProcess fixtures only.
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from conftest import NullIpcDeps, init_test_database, make_settings

from pynchy.host.container_manager.ipc.registry import dispatch
from pynchy.host.container_manager.security.identity import ReceiptVerification
from pynchy.host.git_ops.api import RepoContext
from pynchy.workspace.api import WorkspaceProfile

if TYPE_CHECKING:
    from pathlib import Path

ADMIN_GROUP = WorkspaceProfile(
    jid="admin-1@g.us",
    name="Admin",
    folder="admin-1",
    trigger="always",
    added_at="2024-01-01T00:00:00.000Z",
    is_admin=True,
)

OTHER_GROUP = WorkspaceProfile(
    jid="other@g.us",
    name="Other",
    folder="other-group",
    trigger="@pynchy",
    added_at="2024-01-01T00:00:00.000Z",
)


def _test_settings(*, data_dir=None, project_root=None):
    overrides = {}
    if data_dir is not None:
        overrides["data_dir"] = data_dir
    if project_root is not None:
        overrides["project_root"] = project_root
    return make_settings(**overrides)


class MockDeps(NullIpcDeps):
    """Mock IPC dependencies."""

    def __init__(self, groups: dict[str, WorkspaceProfile]):
        self._groups = groups
        self.broadcast_messages: list[tuple[str, str]] = []
        self.host_messages: list[tuple[str, str]] = []
        self.system_notices: list[tuple[str, str]] = []
        self.cleared_sessions: list[str] = []
        self.cleared_chats: list[str] = []
        self.enqueued_checks: list[str] = []
        self.deploy_calls: list[tuple[str, bool]] = []
        self.requested_deploys: list[tuple[str | None, str, bool, str]] = []

    async def broadcast_to_channels(self, jid: str, event: object) -> None:
        content = event.content if hasattr(event, "content") else str(event)
        self.broadcast_messages.append((jid, content))

    async def broadcast_host_message(self, jid: str, text: str) -> None:
        self.host_messages.append((jid, text))

    async def broadcast_system_notice(self, jid: str, text: str) -> None:
        self.system_notices.append((jid, text))

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return self._groups

    def register_workspace(self, profile: WorkspaceProfile) -> None:
        self._groups[profile.jid] = profile

    async def clear_session(self, group_folder: str) -> None:
        self.cleared_sessions.append(group_folder)

    async def clear_chat_history(self, chat_jid: str) -> None:
        self.cleared_chats.append(chat_jid)

    def enqueue_message_check(self, group_jid: str) -> None:
        self.enqueued_checks.append(group_jid)

    async def request_deploy(
        self,
        *,
        chat_jid: str | None,
        commit_sha: str,
        rebuild: bool,
        resume_prompt: str,
    ) -> None:
        self.requested_deploys.append((chat_jid, commit_sha, rebuild, resume_prompt))

    async def trigger_deploy(self, previous_sha: str, *, rebuild: bool = True) -> None:
        self.deploy_calls.append((previous_sha, rebuild))


@pytest.fixture
async def deps():
    await init_test_database()
    return MockDeps(
        {
            "admin-1@g.us": ADMIN_GROUP,
            "other@g.us": OTHER_GROUP,
        }
    )


# ---------------------------------------------------------------------------
# sync_worktree_to_main IPC handler
# ---------------------------------------------------------------------------


@pytest.mark.action("lifecycle.worktree.sync")
class TestSyncWorktreeToMain:
    """Tests for the sync_worktree_to_main IPC command handler."""

    @pytest.mark.parametrize(
        ("diff_result", "expected_reason"),
        [
            (
                subprocess.CompletedProcess(
                    args=[], returncode=1, stdout="", stderr="fatal: repository unavailable"
                ),
                "Committed patch unavailable for owner/pynchy: fatal: repository unavailable",
            ),
            (
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="GIT binary patch\nliteral 8\n", stderr=""
                ),
                "Committed patch for owner/pynchy contains binary content",
            ),
            (
                subprocess.CompletedProcess(args=[], returncode=0, stdout="x" * 70_000, stderr=""),
                "Committed patch exceeds the Cop inspection context limit",
            ),
        ],
        ids=["git-failure", "binary-patch", "oversized-patch"],
    )
    async def test_unsafe_committed_patch_requires_human_approval(
        self,
        deps: MockDeps,
        tmp_path: Path,
        diff_result: subprocess.CompletedProcess[str],
        expected_reason: str,
    ) -> None:
        """Patch inspection failures never reach PR publication."""
        repo_ctx = RepoContext(
            slug="owner/pynchy", root=tmp_path / "repo", worktrees_dir=tmp_path / "worktrees"
        )
        (repo_ctx.worktrees_dir / "other-group").mkdir(parents=True)
        result_dir = tmp_path / "data" / "ipc" / "other-group" / "merge_results"
        result_dir.mkdir(parents=True)

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=_test_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle._resolve_publication_repos",
                return_value=[repo_ctx],
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.detect_main_branch",
                return_value="main",
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.run_git",
                return_value=diff_result,
            ),
            patch(
                "pynchy.host.container_manager.security.cop_gate.cop_gate",
                new_callable=AsyncMock,
                return_value=False,
            ) as cop,
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_create_pr_from_worktree"
            ) as create_pr,
        ):
            await dispatch(
                {
                    "type": "sync_worktree_to_main",
                    "request_id": "req-unsafe-patch",
                    "publication": "pull-request",
                },
                "other-group",
                False,
                deps,
            )

        assert cop.await_args.kwargs["required_human_reason"] == expected_reason
        create_pr.assert_not_called()

    async def test_invalid_approval_receipt_is_rejected_before_repository_resolution(
        self,
        deps: MockDeps,
        tmp_path: Path,
    ) -> None:
        """A replayed or malformed receipt cannot trigger repository work."""
        result_dir = tmp_path / "data" / "ipc" / "other-group" / "merge_results"
        result_dir.mkdir(parents=True)

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=_test_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.container_manager.security.cop_gate.verify_approval_receipt",
                new_callable=AsyncMock,
                return_value=ReceiptVerification.INVALID,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle._resolve_publication_repos"
            ) as resolve_repos,
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_create_pr_from_worktree"
            ) as create_pr,
        ):
            await dispatch(
                {
                    "type": "sync_worktree_to_main",
                    "request_id": "req-invalid-receipt",
                    "publication": "pull-request",
                },
                "other-group",
                False,
                deps,
            )

        resolve_repos.assert_not_called()
        create_pr.assert_not_called()
        result = json.loads((result_dir / "req-invalid-receipt.json").read_text())
        assert result == {
            "success": False,
            "message": "Publication blocked: invalid or replayed approval receipt.",
        }

    async def test_empty_repository_selection_returns_without_cop_or_publication(
        self,
        deps: MockDeps,
        tmp_path: Path,
    ) -> None:
        """A group with no configured repository gets a deterministic response."""
        result_dir = tmp_path / "data" / "ipc" / "other-group" / "merge_results"
        result_dir.mkdir(parents=True)

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=_test_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle._resolve_publication_repos",
                return_value=[],
            ),
            patch(
                "pynchy.host.container_manager.security.cop_gate.cop_gate",
                new_callable=AsyncMock,
            ) as cop,
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_create_pr_from_worktree"
            ) as create_pr,
        ):
            await dispatch(
                {
                    "type": "sync_worktree_to_main",
                    "request_id": "req-no-repo",
                    "publication": "pull-request",
                },
                "other-group",
                False,
                deps,
            )

        cop.assert_not_awaited()
        create_pr.assert_not_called()
        result = json.loads((result_dir / "req-no-repo.json").read_text())
        assert result == {"success": False, "message": "No repo configured for this group."}

    async def test_writes_result_file_on_success(self, deps: MockDeps, tmp_path: Path):
        """PR publication writes a result JSON for the blocking MCP tool."""
        merge_results_dir = tmp_path / "data" / "ipc" / "other-group" / "merge_results"
        merge_results_dir.mkdir(parents=True)
        fake_repo_ctx = RepoContext(
            slug="owner/pynchy", root=tmp_path, worktrees_dir=tmp_path / "wt"
        )

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=_test_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.git_ops.repo.resolve_repos_for_group",
                return_value=[fake_repo_ctx],
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle._publication_patch_context",
                return_value=("Committed patch:\n+safe change", None),
            ),
            patch(
                "pynchy.host.container_manager.security.cop_gate.cop_gate",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_create_pr_from_worktree",
                return_value={"success": True, "message": "Opened pull request"},
            ),
        ):
            await dispatch(
                {
                    "type": "sync_worktree_to_main",
                    "request_id": "req-123",
                    "publication": "pull-request",
                },
                "other-group",
                False,
                deps,
            )

        result_file = merge_results_dir / "req-123.json"
        assert result_file.exists()
        data = json.loads(result_file.read_text())
        assert data["success"] is True
        assert "Opened pull request" in data["repos"]["owner/pynchy"]["message"]

    async def test_writes_result_file_on_failure(self, deps: MockDeps, tmp_path: Path):
        """PR failure is returned immediately with its repository diagnostic."""
        merge_results_dir = tmp_path / "data" / "ipc" / "other-group" / "merge_results"
        merge_results_dir.mkdir(parents=True)
        fake_repo_ctx = RepoContext(
            slug="owner/pynchy", root=tmp_path, worktrees_dir=tmp_path / "wt"
        )

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=_test_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.git_ops.repo.resolve_repos_for_group",
                return_value=[fake_repo_ctx],
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle._publication_patch_context",
                return_value=("Committed patch:\n+safe change", None),
            ),
            patch(
                "pynchy.host.container_manager.security.cop_gate.cop_gate",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_create_pr_from_worktree",
                return_value={"success": False, "message": "GitHub returned HTTP 403"},
            ),
        ):
            await dispatch(
                {
                    "type": "sync_worktree_to_main",
                    "request_id": "req-fail",
                    "publication": "pull-request",
                },
                "other-group",
                False,
                deps,
            )

        result_file = merge_results_dir / "req-fail.json"
        assert result_file.exists()
        data = json.loads(result_file.read_text())
        assert data["success"] is False
        assert "HTTP 403" in data["repos"]["owner/pynchy"]["message"]
        assert deps.deploy_calls == []


# ---------------------------------------------------------------------------
# Deploy edge cases
# ---------------------------------------------------------------------------


class TestDeployEdgeCases:
    """Tests for deploy command edge cases in the IPC handler."""

    async def test_deploy_without_chat_jid_delegates_notification_resolution(self, deps: MockDeps):
        """The composition root resolves a missing notification target."""
        await dispatch(
            {
                "type": "deploy",
                "rebuildContainer": False,
                "resumePrompt": "Done.",
                "headSha": "abc123",
            },
            "admin-1",
            True,
            deps,
        )
        assert deps.requested_deploys == [(None, "abc123", False, "Done.")]

    async def test_deploy_with_rebuild_but_no_build_script(self, deps: MockDeps, tmp_path: Path):
        """Deploy rebuild is represented on the Temporal request, not run inline."""
        await dispatch(
            {
                "type": "deploy",
                "rebuildContainer": True,
                "resumePrompt": "Done.",
                "headSha": "abc123",
                "chatJid": "admin-1@g.us",
            },
            "admin-1",
            True,
            deps,
        )
        assert deps.requested_deploys == [("admin-1@g.us", "abc123", True, "Done.")]

    async def test_deploy_uses_default_resume_prompt(self, deps: MockDeps):
        """Deploy with no resumePrompt should use the default."""
        await dispatch(
            {
                "type": "deploy",
                "rebuildContainer": False,
                "headSha": "abc123",
                "chatJid": "admin-1@g.us",
                # resumePrompt intentionally missing
            },
            "admin-1",
            True,
            deps,
        )
        assert "Deploy complete" in deps.requested_deploys[0][3]


# ---------------------------------------------------------------------------
# IPC type edge cases
# ---------------------------------------------------------------------------


class TestIpcTypeEdgeCases:
    """Edge cases in the IPC type matching."""

    async def test_empty_type_field_is_unknown(self, deps: MockDeps):
        """A task with no type field should be handled as unknown."""
        # Should not raise
        await dispatch({"no_type_field": True}, "admin-1", True, deps)

    async def test_none_type_field_is_unknown(self, deps: MockDeps):
        """A task with type=None should be handled gracefully."""
        await dispatch({"type": None}, "admin-1", True, deps)

    async def test_empty_data_dict_is_handled(self, deps: MockDeps):
        """An empty data dict should not crash the processor."""
        await dispatch({}, "admin-1", True, deps)

    async def test_unknown_type_does_not_raise(self, deps: MockDeps):
        """An unrecognized IPC type should be logged but not raise."""
        await dispatch({"type": "totally_made_up_command"}, "admin-1", True, deps)
