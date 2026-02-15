"""Tests for IPC sync_worktree_to_main and deploy edge cases.

These test the process_task_ipc match branches for sync_worktree_to_main and deploy
that aren't covered by test_ipc_auth.py (which focuses on authorization) or
test_ipc_watcher.py (which focuses on the file scanning loop).

Key coverage gaps addressed:
- sync_worktree_to_main result file writing
- sync_worktree_to_main notification on success vs failure
- deploy fallback when chatJid is missing
- deploy with no god group registered
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.config import (
    AgentConfig,
    CommandWordsConfig,
    ContainerConfig,
    IntervalsConfig,
    LoggingConfig,
    QueueConfig,
    SchedulerConfig,
    SecretsConfig,
    SecurityConfig,
    ServerConfig,
    Settings,
    WorkspaceDefaultsConfig,
)
from pynchy.db import _init_test_database
from pynchy.ipc import _handle_deploy, process_task_ipc
from pynchy.types import RegisteredGroup

GOD_GROUP = RegisteredGroup(
    name="God",
    folder="god",
    trigger="always",
    added_at="2024-01-01T00:00:00.000Z",
    is_god=True,
)

OTHER_GROUP = RegisteredGroup(
    name="Other",
    folder="other-group",
    trigger="@pynchy",
    added_at="2024-01-01T00:00:00.000Z",
)


def _test_settings(*, data_dir=None, project_root=None):
    s = Settings.model_construct(
        agent=AgentConfig(),
        container=ContainerConfig(),
        server=ServerConfig(),
        logging=LoggingConfig(),
        secrets=SecretsConfig(),
        workspace_defaults=WorkspaceDefaultsConfig(),
        workspaces={},
        commands=CommandWordsConfig(),
        scheduler=SchedulerConfig(),
        intervals=IntervalsConfig(),
        queue=QueueConfig(),
        security=SecurityConfig(),
    )
    if data_dir is not None:
        s.__dict__["data_dir"] = data_dir
    if project_root is not None:
        s.__dict__["project_root"] = project_root
    return s


class MockDeps:
    """Mock IPC dependencies."""

    def __init__(self, groups: dict[str, RegisteredGroup]):
        self._groups = groups
        self.broadcast_messages: list[tuple[str, str]] = []
        self.host_messages: list[tuple[str, str]] = []
        self.system_notices: list[tuple[str, str]] = []
        self.cleared_sessions: list[str] = []
        self.cleared_chats: list[str] = []
        self.enqueued_checks: list[str] = []

    async def broadcast_to_channels(self, jid: str, text: str) -> None:
        self.broadcast_messages.append((jid, text))

    async def broadcast_host_message(self, jid: str, text: str) -> None:
        self.host_messages.append((jid, text))

    async def broadcast_system_notice(self, jid: str, text: str) -> None:
        self.system_notices.append((jid, text))

    def registered_groups(self) -> dict[str, RegisteredGroup]:
        return self._groups

    def register_group(self, jid: str, group: RegisteredGroup) -> None:
        self._groups[jid] = group

    async def sync_group_metadata(self, force: bool) -> None:
        pass

    async def get_available_groups(self) -> list[Any]:
        return []

    def write_groups_snapshot(
        self,
        group_folder: str,
        is_god: bool,
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


@pytest.fixture
async def deps():
    await _init_test_database()
    return MockDeps(
        {
            "god@g.us": GOD_GROUP,
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

        with (
            patch(
                "pynchy.ipc.get_settings",
                return_value=_test_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.ipc.host_sync_worktree",
                return_value={"success": True, "message": "Merged 1 commit(s)"},
            ),
            patch("pynchy.ipc.host_notify_worktree_updates", new_callable=AsyncMock),
        ):
            await process_task_ipc(
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
                "pynchy.ipc.get_settings",
                return_value=_test_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.ipc.host_sync_worktree",
                return_value={"success": False, "message": "uncommitted changes"},
            ),
            patch("pynchy.ipc.host_notify_worktree_updates", new_callable=AsyncMock),
        ):
            await process_task_ipc(
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

        with (
            patch(
                "pynchy.ipc.get_settings",
                return_value=_test_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.ipc.host_sync_worktree",
                return_value={"success": True, "message": "done"},
            ),
            patch("pynchy.ipc.host_notify_worktree_updates", new_callable=AsyncMock) as mock_notify,
        ):
            await process_task_ipc(
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
                "pynchy.ipc.get_settings",
                return_value=_test_settings(data_dir=tmp_path / "data"),
            ),
            patch(
                "pynchy.ipc.host_sync_worktree",
                return_value={"success": False, "message": "conflict"},
            ),
            patch("pynchy.ipc.host_notify_worktree_updates", new_callable=AsyncMock) as mock_notify,
        ):
            await process_task_ipc(
                {
                    "type": "sync_worktree_to_main",
                    "requestId": "req-789",
                },
                "other-group",
                False,
                deps,
            )

        mock_notify.assert_not_called()


# ---------------------------------------------------------------------------
# Deploy edge cases
# ---------------------------------------------------------------------------


class TestDeployEdgeCases:
    """Tests for deploy command edge cases in the IPC handler."""

    async def test_deploy_without_chat_jid_uses_god_group(self, deps: MockDeps):
        """Deploy request missing chatJid should fall back to god group's JID."""
        with patch("pynchy.ipc.finalize_deploy", new_callable=AsyncMock) as mock_finalize:
            await _handle_deploy(
                {
                    "rebuildContainer": False,
                    "resumePrompt": "Done.",
                    "headSha": "abc123",
                    # chatJid intentionally missing
                },
                "god",
                deps,
            )
            mock_finalize.assert_called_once()
            # Should have resolved the god group's JID
            assert mock_finalize.call_args.kwargs["chat_jid"] == "god@g.us"

    async def test_deploy_without_chat_jid_and_no_god_group(self):
        """Deploy request with no chatJid and no god group should not finalize."""
        await _init_test_database()
        # Deps with no god group
        no_god_deps = MockDeps({"other@g.us": OTHER_GROUP})

        with patch("pynchy.ipc.finalize_deploy", new_callable=AsyncMock) as mock_finalize:
            await _handle_deploy(
                {
                    "rebuildContainer": False,
                    "headSha": "abc123",
                },
                "god",
                no_god_deps,
            )
            mock_finalize.assert_not_called()

    async def test_deploy_with_rebuild_but_no_build_script(self, deps: MockDeps, tmp_path: Path):
        """Deploy requesting rebuild when build.sh doesn't exist should still finalize."""
        with (
            patch(
                "pynchy.ipc.get_settings",
                return_value=_test_settings(project_root=tmp_path),
            ),
            patch("pynchy.ipc.finalize_deploy", new_callable=AsyncMock) as mock_finalize,
        ):
            await _handle_deploy(
                {
                    "rebuildContainer": True,
                    "resumePrompt": "Done.",
                    "headSha": "abc123",
                    "chatJid": "god@g.us",
                },
                "god",
                deps,
            )
            # Should still finalize since build.sh not found is non-fatal
            mock_finalize.assert_called_once()

    async def test_deploy_uses_default_resume_prompt(self, deps: MockDeps):
        """Deploy with no resumePrompt should use the default."""
        with patch("pynchy.ipc.finalize_deploy", new_callable=AsyncMock) as mock_finalize:
            await _handle_deploy(
                {
                    "rebuildContainer": False,
                    "headSha": "abc123",
                    "chatJid": "god@g.us",
                    # resumePrompt intentionally missing
                },
                "god",
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
        await process_task_ipc({"no_type_field": True}, "god", True, deps)

    async def test_none_type_field_is_unknown(self, deps: MockDeps):
        """A task with type=None should be handled gracefully."""
        await process_task_ipc({"type": None}, "god", True, deps)

    async def test_empty_data_dict_is_handled(self, deps: MockDeps):
        """An empty data dict should not crash the processor."""
        await process_task_ipc({}, "god", True, deps)

    async def test_unknown_type_does_not_raise(self, deps: MockDeps):
        """An unrecognized IPC type should be logged but not raise."""
        await process_task_ipc({"type": "totally_made_up_command"}, "god", True, deps)
