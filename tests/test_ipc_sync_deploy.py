"""Tests for IPC sync_worktree_to_main and deploy edge cases.

These test the dispatch match branches for sync_worktree_to_main and deploy
that aren't covered by test_ipc_auth.py (which focuses on authorization) or
test_ipc_watcher.py (which focuses on the file scanning loop).

Key coverage gaps addressed:
- sync_worktree_to_main result file writing
- sync_worktree_to_main notification on success vs failure
- deploy fallback when chatJid is missing
- deploy with no admin group registered
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from conftest import make_settings

from pynchy.host.container_manager.ipc import dispatch
from pynchy.host.container_manager.ipc.handlers_deploy import _handle_deploy
from pynchy.host.git_ops.repo import RepoContext
from pynchy.state import _init_test_database
from pynchy.types import WorkspaceProfile

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


class MockDeps:
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

    async def broadcast_to_channels(self, jid: str, text: str) -> None:
        self.broadcast_messages.append((jid, text))

    async def broadcast_host_message(self, jid: str, text: str) -> None:
        self.host_messages.append((jid, text))

    async def broadcast_system_notice(self, jid: str, text: str) -> None:
        self.system_notices.append((jid, text))

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return self._groups

    def register_workspace(self, profile: WorkspaceProfile) -> None:
        self._groups[profile.jid] = profile

    async def sync_group_metadata(self, force: bool) -> None:
        pass

    async def get_available_groups(self) -> list[Any]:
        return []

    def write_groups_snapshot(
        self,
        group_folder: str,
        is_admin: bool,
        available_groups: list[Any],
        registered_jids: set[str],
    ) -> None:
        pass

    async def clear_session(self, group_folder: str) -> None:
        self.cleared_sessions.append(group_folder)

    async def clear_chat_history(self, chat_jid: str) -> None:
        self.cleared_chats.append(chat_jid)

    def enqueue_message_check(self, group_jid: str) -> None:
        self.enqueued_checks.append(group_jid)

    def channels(self) -> list:
        return []

    def get_active_sessions(self) -> dict[str, str]:
        return {}

    async def trigger_deploy(self, previous_sha: str, rebuild: bool = True) -> None:
        self.deploy_calls.append((previous_sha, rebuild))


@pytest.fixture
async def deps():
    await _init_test_database()
    return MockDeps(
        {
            "admin-1@g.us": ADMIN_GROUP,
            "other@g.us": OTHER_GROUP,
        }
    )


# ---------------------------------------------------------------------------
# sync_worktree_to_main IPC handler
# ---------------------------------------------------------------------------


class TestSyncWorktreeToMain:
    """Tests for the sync_worktree_to_main IPC command handler."""

    async def test_writes_result_file_on_success(self, deps: MockDeps, tmp_path: Path):
        """sync_worktree_to_main should write a result JSON for the blocking MCP tool."""
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
                "pynchy.host.git_ops.repo.resolve_repo_for_group",
                return_value=fake_repo_ctx,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_sync_worktree",
                return_value={"success": True, "message": "Merged 1 commit(s)"},
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_notify_worktree_updates",
                new_callable=AsyncMock,
            ),
        ):
            await dispatch(
                {
                    "type": "sync_worktree_to_main",
                    "requestId": "req-123",
                },
                "other-group",
                False,
                deps,
            )

        result_file = merge_results_dir / "req-123.json"
        assert result_file.exists()
        data = json.loads(result_file.read_text())
        assert data["success"] is True
        assert "Merged" in data["message"]

    async def test_writes_result_file_on_failure(self, deps: MockDeps, tmp_path: Path):
        """Failure result should also be written so the MCP tool can read it."""
        merge_results_dir = tmp_path / "data" / "ipc" / "other-group" / "merge_results"
        merge_results_dir.mkdir(parents=True)

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=_test_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_sync_worktree",
                return_value={"success": False, "message": "uncommitted changes"},
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_notify_worktree_updates",
                new_callable=AsyncMock,
            ),
        ):
            await dispatch(
                {
                    "type": "sync_worktree_to_main",
                    "requestId": "req-fail",
                },
                "other-group",
                False,
                deps,
            )

        result_file = merge_results_dir / "req-fail.json"
        assert result_file.exists()
        data = json.loads(result_file.read_text())
        assert data["success"] is False

    async def test_notifies_other_worktrees_on_success(self, deps: MockDeps, tmp_path: Path):
        """On successful sync, other worktrees should be notified of changes."""
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
                "pynchy.host.git_ops.repo.resolve_repo_for_group",
                return_value=fake_repo_ctx,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_sync_worktree",
                return_value={"success": True, "message": "done"},
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_notify_worktree_updates",
                new_callable=AsyncMock,
            ) as mock_notify,
        ):
            await dispatch(
                {
                    "type": "sync_worktree_to_main",
                    "requestId": "req-456",
                },
                "other-group",
                False,
                deps,
            )

        mock_notify.assert_called_once()
        # Source group should be the first positional arg
        assert mock_notify.call_args[0][0] == "other-group"

    async def test_skips_notification_on_failure(self, deps: MockDeps, tmp_path: Path):
        """On failed sync, worktree notification should be skipped."""
        merge_results_dir = tmp_path / "data" / "ipc" / "other-group" / "merge_results"
        merge_results_dir.mkdir(parents=True)

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=_test_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_sync_worktree",
                return_value={"success": False, "message": "conflict"},
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_notify_worktree_updates",
                new_callable=AsyncMock,
            ) as mock_notify,
        ):
            await dispatch(
                {
                    "type": "sync_worktree_to_main",
                    "requestId": "req-789",
                },
                "other-group",
                False,
                deps,
            )

        mock_notify.assert_not_called()

    async def test_sync_worktree_triggers_deploy_on_src_changes(
        self, deps: MockDeps, tmp_path: Path
    ):
        """Successful sync with src/ changes should trigger deploy."""
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
                "pynchy.host.git_ops.repo.resolve_repo_for_group",
                return_value=fake_repo_ctx,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_sync_worktree",
                return_value={"success": True, "message": "Merged 1 commit(s)"},
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_notify_worktree_updates",
                new_callable=AsyncMock,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_head_sha",
                side_effect=["pre-sha-111", "post-sha-222"],
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.needs_deploy",
                return_value=True,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.needs_container_rebuild",
                return_value=False,
            ),
        ):
            await dispatch(
                {
                    "type": "sync_worktree_to_main",
                    "requestId": "req-deploy",
                },
                "other-group",
                False,
                deps,
            )

        assert len(deps.deploy_calls) == 1
        sha, rebuild = deps.deploy_calls[0]
        assert sha == "pre-sha-111"
        assert rebuild is False

    async def test_sync_worktree_no_deploy_on_irrelevant_changes(
        self, deps: MockDeps, tmp_path: Path
    ):
        """Successful sync with only docs changes should not trigger deploy."""
        merge_results_dir = tmp_path / "data" / "ipc" / "other-group" / "merge_results"
        merge_results_dir.mkdir(parents=True)

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=_test_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_sync_worktree",
                return_value={"success": True, "message": "Merged 1 commit(s)"},
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.host_notify_worktree_updates",
                new_callable=AsyncMock,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_head_sha",
                side_effect=["pre-sha-111", "post-sha-222"],
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.needs_deploy",
                return_value=False,
            ),
        ):
            await dispatch(
                {
                    "type": "sync_worktree_to_main",
                    "requestId": "req-nodeploy",
                },
                "other-group",
                False,
                deps,
            )

        assert len(deps.deploy_calls) == 0


# ---------------------------------------------------------------------------
# Deploy edge cases
# ---------------------------------------------------------------------------


class TestDeployEdgeCases:
    """Tests for deploy command edge cases in the IPC handler."""

    async def test_deploy_without_chat_jid_uses_admin_group(self, deps: MockDeps):
        """Deploy request missing chatJid should fall back to admin group's JID."""
        with patch(
            "pynchy.host.container_manager.ipc.handlers_deploy.finalize_deploy",
            new_callable=AsyncMock,
        ) as mock_finalize:
            await _handle_deploy(
                {
                    "rebuildContainer": False,
                    "resumePrompt": "Done.",
                    "headSha": "abc123",
                    # chatJid intentionally missing
                },
                "admin-1",
                True,
                deps,
            )
            mock_finalize.assert_called_once()
            # Should have resolved the admin group's JID
            assert mock_finalize.call_args.kwargs["chat_jid"] == "admin-1@g.us"

    async def test_deploy_without_chat_jid_and_no_admin_group(self):
        """Deploy request with no chatJid and no admin group should not finalize."""
        await _init_test_database()
        # Deps with no admin group
        no_admin_deps = MockDeps({"other@g.us": OTHER_GROUP})

        with patch(
            "pynchy.host.container_manager.ipc.handlers_deploy.finalize_deploy",
            new_callable=AsyncMock,
        ) as mock_finalize:
            await _handle_deploy(
                {
                    "rebuildContainer": False,
                    "headSha": "abc123",
                },
                "admin-1",
                True,
                no_admin_deps,
            )
            mock_finalize.assert_not_called()

    async def test_deploy_with_rebuild_but_no_build_script(self, deps: MockDeps, tmp_path: Path):
        """Deploy requesting rebuild when build.sh doesn't exist should still finalize."""
        from pynchy.host.orchestrator.deploy import BuildResult

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_deploy.build_container_image",
                return_value=BuildResult(success=True, skipped=True),
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_deploy.finalize_deploy",
                new_callable=AsyncMock,
            ) as mock_finalize,
        ):
            await _handle_deploy(
                {
                    "rebuildContainer": True,
                    "resumePrompt": "Done.",
                    "headSha": "abc123",
                    "chatJid": "admin-1@g.us",
                },
                "admin-1",
                True,
                deps,
            )
            # Should still finalize since build.sh not found is non-fatal (skipped)
            mock_finalize.assert_called_once()

    async def test_deploy_uses_default_resume_prompt(self, deps: MockDeps):
        """Deploy with no resumePrompt should use the default."""
        with patch(
            "pynchy.host.container_manager.ipc.handlers_deploy.finalize_deploy",
            new_callable=AsyncMock,
        ) as mock_finalize:
            await _handle_deploy(
                {
                    "rebuildContainer": False,
                    "headSha": "abc123",
                    "chatJid": "admin-1@g.us",
                    # resumePrompt intentionally missing
                },
                "admin-1",
                True,
                deps,
            )
            mock_finalize.assert_called_once()
            assert "Deploy complete" in mock_finalize.call_args.kwargs["resume_prompt"]


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
