"""Integration tests: cop_gate wired into host-mutating IPC handlers.

Verifies that the host-mutating IPC handlers calls cop_gate()
before executing its side effects.  When cop_gate returns False (flagged),
the handler must bail without performing the mutation.

Tested handlers:
  - sync_worktree_to_main  (_handlers_lifecycle.py)
  - register_group          (_handlers_groups.py)
  - schedule_host_job       (_handlers_tasks.py)
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from conftest import NullIpcDeps, init_test_database, make_settings

from pynchy.host.container_manager.ipc.registry import dispatch
from pynchy.host.container_manager.security.identity import ReceiptVerification
from pynchy.host.git_ops.api import RepoContext
from pynchy.state import get_all_host_jobs
from pynchy.workspace.api import WorkspaceProfile

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

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


class MockDeps(NullIpcDeps):
    """Minimal mock satisfying the IpcDeps protocol."""

    def __init__(self, groups: dict[str, WorkspaceProfile] | None = None):
        self._groups = groups or {}
        self.broadcast_messages: list[tuple[str, str]] = []
        self.host_messages: list[tuple[str, str]] = []
        self.system_notices: list[tuple[str, str]] = []
        self.registered: list[WorkspaceProfile] = []
        self._channels: list[Any] = []

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
        self.registered.append(profile)
        self._groups[profile.jid] = profile

    async def sync_group_metadata(self, *, force: bool) -> None:
        pass

    async def get_available_groups(self) -> list[Any]:
        return []

    def write_groups_snapshot(
        self,
        group_folder: str,
        available_groups: list[Any],
        registered_jids: set[str],
        *,
        is_admin: bool,
    ) -> None:
        pass

    def has_active_session(self, group_folder: str) -> bool:
        return False

    async def clear_session(self, group_folder: str) -> None:
        pass

    def get_active_sessions(self) -> dict[str, str]:
        return {}

    async def clear_chat_history(self, chat_jid: str) -> None:
        pass

    def enqueue_message_check(self, group_jid: str) -> None:
        pass

    def channels(self) -> list:
        return self._channels

    async def request_deploy(
        self,
        *,
        chat_jid: str | None,
        commit_sha: str,
        rebuild: bool,
        resume_prompt: str,
    ) -> None:
        del chat_jid, commit_sha, rebuild, resume_prompt

    async def trigger_deploy(self, previous_sha: str, *, rebuild: bool = True) -> None:
        pass


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
# 1. sync_worktree_to_main
# ---------------------------------------------------------------------------


class TestSyncWorktreeCopGate:
    """sync_worktree_to_main should call cop_gate and block on flag."""

    async def test_blocked_by_cop_returns_prompt_failure(self, deps, tmp_path):
        """A blocked publication returns to the caller instead of timing out."""
        repo_ctx = RepoContext("owner/repo", tmp_path, tmp_path / "worktrees")
        with (
            patch(
                "pynchy.host.container_manager.security.cop_gate.cop_gate",
                new_callable=AsyncMock,
                return_value=False,
            ) as mock_cop,
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.git_ops.repo.resolve_repos_for_group",
                return_value=[repo_ctx],
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle._publication_patch_context",
                return_value=("Committed patch:\n+safe change", None),
            ),
        ):
            await dispatch(
                {
                    "type": "sync_worktree_to_main",
                    "request_id": "req-1",
                    "publication": "pull-request",
                },
                "admin-1",
                True,
                deps,
            )

        mock_cop.assert_called_once()
        assert mock_cop.call_args.args[0] == "sync_worktree_to_main"
        result_file = tmp_path / "data" / "ipc" / "admin-1" / "merge_results" / "req-1.json"
        result = json.loads(result_file.read_text())
        assert result["success"] is False
        assert "requires human approval" in result["message"]

    async def test_cop_receives_request_id(self, deps, tmp_path):
        """sync_worktree_to_main passes request_id to cop_gate (request-reply)."""
        repo_ctx = RepoContext("owner/repo", tmp_path, tmp_path / "worktrees")
        with (
            patch(
                "pynchy.host.container_manager.security.cop_gate.cop_gate",
                new_callable=AsyncMock,
                return_value=False,
            ) as mock_cop,
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.git_ops.repo.resolve_repos_for_group",
                return_value=[repo_ctx],
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle._publication_patch_context",
                return_value=("Committed patch:\n+safe change", None),
            ),
        ):
            await dispatch(
                {
                    "type": "sync_worktree_to_main",
                    "request_id": "req-42",
                    "publication": "pull-request",
                },
                "admin-1",
                True,
                deps,
            )

        assert mock_cop.call_args.kwargs.get("request_id") == "req-42"

    async def test_missing_patch_context_forces_human_approval(self, deps, tmp_path):
        repo_ctx = RepoContext("owner/repo", tmp_path, tmp_path / "missing-worktrees")
        with (
            patch(
                "pynchy.host.container_manager.security.cop_gate.cop_gate",
                new_callable=AsyncMock,
                return_value=False,
            ) as cop,
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.git_ops.repo.resolve_repos_for_group",
                return_value=[repo_ctx],
            ),
        ):
            await dispatch(
                {
                    "type": "sync_worktree_to_main",
                    "request_id": "req-missing-patch",
                    "publication": "pull-request",
                },
                "admin-1",
                True,
                deps,
            )

        reason = cop.await_args.kwargs["required_human_reason"]
        assert reason == "Committed patch unavailable for owner/repo: worktree is missing"

    @pytest.mark.parametrize("publication", [None, "merge-to-main", "deploy"])
    async def test_non_pr_publication_cannot_reach_cop_or_host_mutation(
        self,
        deps,
        tmp_path,
        publication,
    ):
        """Missing or forged publication modes fail before authority inspection."""
        request = {
            "type": "sync_worktree_to_main",
            "request_id": "req-policy",
        }
        if publication is not None:
            request["publication"] = publication
        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.container_manager.security.cop_gate.cop_gate",
                new_callable=AsyncMock,
            ) as cop,
            patch(
                "pynchy.host.git_ops.repo.resolve_repos_for_group",
            ) as resolve_repos,
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_create_pr_from_worktree",
            ) as create_pr,
        ):
            await dispatch(request, "admin-1", True, deps)

        cop.assert_not_awaited()
        resolve_repos.assert_not_called()
        create_pr.assert_not_called()
        result_file = tmp_path / "data" / "ipc" / "admin-1" / "merge_results" / "req-policy.json"
        result = json.loads(result_file.read_text())
        assert result["success"] is False
        assert "Direct merge and deployment are not authorized" in result["message"]

    async def test_caller_asserted_approval_does_not_skip_gate(self, deps, tmp_path):
        """An untrusted caller boolean cannot bypass Cop inspection."""
        repo_ctx = RepoContext("owner/repo", tmp_path, tmp_path / "worktrees")
        with (
            patch(
                "pynchy.host.container_manager.security.cop_gate.cop_gate",
                new_callable=AsyncMock,
                return_value=False,
            ) as mock_cop,
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=make_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.git_ops.repo.resolve_repos_for_group",
                return_value=[repo_ctx],
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle._publication_patch_context",
                return_value=("Committed patch:\n+safe change", None),
            ),
            patch("pynchy.host.container_manager.ipc.handlers_lifecycle.write_ipc_response"),
        ):
            await dispatch(
                {
                    "type": "sync_worktree_to_main",
                    "request_id": "req-ok",
                    "publication": "pull-request",
                    "_cop_approved": True,
                },
                "admin-1",
                True,
                deps,
            )

        mock_cop.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2. register_group
# ---------------------------------------------------------------------------


class TestRegisterGroupCopGate:
    """register_group should call cop_gate and block on flag."""

    async def test_valid_receipt_registers_without_rechecking_cop(self, deps):
        """A valid approval receipt authorizes registration without another gate."""
        with (
            patch(
                "pynchy.host.container_manager.security.cop_gate.verify_approval_receipt",
                new_callable=AsyncMock,
                return_value=ReceiptVerification.VALID,
            ),
            patch(
                "pynchy.host.container_manager.security.cop_gate.cop_gate",
                new_callable=AsyncMock,
            ) as mock_cop,
        ):
            await dispatch(
                {
                    "type": "register_group",
                    "jid": "approved@g.us",
                    "name": "Approved Group",
                    "folder": "approved",
                    "trigger": "@pynchy",
                },
                "admin-1",
                True,
                deps,
            )

        mock_cop.assert_not_awaited()
        assert deps.workspaces()["approved@g.us"].name == "Approved Group"

    async def test_blocked_by_cop_skips_registration(self, deps):
        """When cop_gate returns False, register_workspace is not called."""
        with patch(
            "pynchy.host.container_manager.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            return_value=False,
        ) as mock_cop:
            await dispatch(
                {
                    "type": "register_group",
                    "jid": "new@g.us",
                    "name": "Evil Group",
                    "folder": "evil-group",
                    "trigger": "@evil",
                },
                "admin-1",
                True,
                deps,
            )

        mock_cop.assert_called_once()
        assert mock_cop.call_args.args[0] == "register_group"
        # Group should NOT have been registered
        assert "new@g.us" not in deps.workspaces()
        assert len(deps.registered) == 0

    async def test_caller_asserted_approval_does_not_skip_gate(self, deps):
        """An untrusted caller boolean cannot bypass Cop inspection."""
        with patch(
            "pynchy.host.container_manager.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            return_value=False,
        ) as mock_cop:
            await dispatch(
                {
                    "type": "register_group",
                    "jid": "approved@g.us",
                    "name": "Approved Group",
                    "folder": "approved",
                    "trigger": "@pynchy",
                    "_cop_approved": True,
                },
                "admin-1",
                True,
                deps,
            )

        mock_cop.assert_awaited_once()
        assert "approved@g.us" not in deps.workspaces()

    async def test_summary_includes_key_fields(self, deps):
        """cop_gate summary should contain name, folder, and trigger."""
        with patch(
            "pynchy.host.container_manager.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            return_value=False,
        ) as mock_cop:
            await dispatch(
                {
                    "type": "register_group",
                    "jid": "new@g.us",
                    "name": "Test Group",
                    "folder": "test-folder",
                    "trigger": "@bot",
                },
                "admin-1",
                True,
                deps,
            )

        summary = mock_cop.call_args.args[1]
        assert "Test Group" in summary
        assert "test-folder" in summary
        assert "@bot" in summary


# ---------------------------------------------------------------------------
# schedule_host_job
# ---------------------------------------------------------------------------


class TestScheduleHostJobCopGate:
    """schedule_host_job should call cop_gate and block on flag."""

    async def test_blocked_by_cop_creates_no_job(self, deps):
        """When cop_gate returns False, no host job is created."""
        with patch(
            "pynchy.host.container_manager.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            return_value=False,
        ) as mock_cop:
            await dispatch(
                {
                    "type": "schedule_host_job",
                    "name": "evil-job",
                    "command": "rm -rf /",
                    "schedule_type": "cron",
                    "schedule_value": "0 3 * * *",
                },
                "admin-1",
                True,
                deps,
            )

        mock_cop.assert_called_once()
        assert mock_cop.call_args.args[0] == "schedule_host_job"
        assert len(await get_all_host_jobs()) == 0

    async def test_summary_includes_command_and_schedule(self, deps):
        """cop_gate summary should include job name, command, and schedule."""
        with patch(
            "pynchy.host.container_manager.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            return_value=False,
        ) as mock_cop:
            await dispatch(
                {
                    "type": "schedule_host_job",
                    "name": "backup-job",
                    "command": "pg_dump mydb > backup.sql",
                    "schedule_type": "interval",
                    "schedule_value": "3600000",
                },
                "admin-1",
                True,
                deps,
            )

        summary = mock_cop.call_args.args[1]
        assert "backup-job" in summary
        assert "pg_dump" in summary
        assert "interval" in summary

    async def test_caller_asserted_approval_does_not_skip_gate(self, deps):
        """An untrusted caller boolean cannot bypass Cop inspection."""
        with patch(
            "pynchy.host.container_manager.security.cop_gate.cop_gate",
            new_callable=AsyncMock,
            return_value=False,
        ) as mock_cop:
            await dispatch(
                {
                    "type": "schedule_host_job",
                    "name": "approved-job",
                    "command": "echo hello",
                    "schedule_type": "cron",
                    "schedule_value": "0 9 * * *",
                    "_cop_approved": True,
                },
                "admin-1",
                True,
                deps,
            )

        mock_cop.assert_awaited_once()
        assert len(await get_all_host_jobs()) == 0
