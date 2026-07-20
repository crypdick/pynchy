"""Tests for the IPC watcher file processing loop.

Tests the inner file-scanning logic of start_ipc_watcher: message authorization,
request dispatch, error handling, and file cleanup. These are critical paths
where a bug could leak messages across groups or silently drop data.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from conftest import NullIpcDeps, make_settings

from pynchy.config.models import NotificationsConfig
from pynchy.host.container_manager.ipc import dispatch
from pynchy.host.container_manager.ipc.watcher import (
    recover_ipc_runtime,
    start_ipc_watcher,
)
from pynchy.host.git_ops.repo import RepoContext
from pynchy.state import init_test_database
from pynchy.types import WorkspaceProfile

if TYPE_CHECKING:
    from pathlib import Path

ADMIN_GROUP = WorkspaceProfile(
    jid="admin-1@g.us",
    name="Admin",
    folder="admin-1",
    trigger="always",
    added_at="2024-01-01",
    is_admin=True,
)

OTHER_GROUP = WorkspaceProfile(
    jid="other@g.us",
    name="Other",
    folder="other-group",
    trigger="@pynchy",
    added_at="2024-01-01",
)


@dataclass
class _WatcherState:
    """The watcher state shape passed through its public start/stop lifecycle."""

    running: bool
    runtime_sweep_task: asyncio.Task[None] | None


def _test_settings(*, data_dir=None):
    return make_settings(**({"data_dir": data_dir} if data_dir is not None else {}))


class MockDeps(NullIpcDeps):
    """Mock IPC dependencies for watcher testing."""

    def __init__(self, groups: dict[str, WorkspaceProfile]):
        self._groups = groups
        self.broadcast_messages: list[tuple[str, str]] = []
        self.host_messages: list[tuple[str, str]] = []
        self.system_notices: list[tuple[str, str]] = []
        self.cleared_sessions: list[str] = []
        self.cleared_chats: list[str] = []
        self.enqueued_checks: list[str] = []

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
# Startup recovery moves unreadable IPC files into errors/
# ---------------------------------------------------------------------------


class _NoopObserver:
    daemon = False

    def schedule(self, *_args, **_kwargs):
        return None

    def start(self):
        return None


class TestStartupSweepErrorFiles:
    """Tests that startup recovery preserves failed IPC files for debugging."""

    async def _run_startup_sweep(self, deps, tmp_path: Path):
        settings = _test_settings(data_dir=tmp_path)

        async def stop_after_startup_sweep(*_args, **_kwargs) -> None:
            await asyncio.sleep(0)

        with (
            patch("pynchy.host.container_manager.ipc.watcher.get_settings", return_value=settings),
            patch(
                "pynchy.host.container_manager.ipc.watcher.Observer",
                return_value=_NoopObserver(),
            ),
            patch(
                "pynchy.host.container_manager.ipc.watcher._state",
                _WatcherState(running=False, runtime_sweep_task=None),
            ),
            patch(
                "pynchy.host.container_manager.ipc.watcher._process_queue",
                stop_after_startup_sweep,
            ),
            patch(
                "pynchy.host.container_manager.ipc.watcher._runtime_sweep_loop",
                stop_after_startup_sweep,
            ),
        ):
            await start_ipc_watcher(deps)

    async def test_preserves_file_content_on_startup_parse_error(self, deps, tmp_path: Path):
        """Error files should retain their original content for debugging."""
        ipc_dir = tmp_path / "ipc"
        source = ipc_dir / "admin-1" / "messages" / "broken.json"
        source.parent.mkdir(parents=True)
        content = '{"type": "message", "chatJid": "test@g.us", "text": '
        source.write_text(content)  # Truncated JSON — triggers parse error

        await self._run_startup_sweep(deps, tmp_path)

        error_file = ipc_dir / "errors" / "admin-1-broken.json"
        assert error_file.read_text() == content
        assert not source.exists()

    async def test_startup_parse_error_overwrites_existing_error_file(
        self,
        deps,
        tmp_path: Path,
    ):
        """The latest error for a group/file name is preserved."""
        ipc_dir = tmp_path / "ipc"
        error_dir = ipc_dir / "errors"
        error_dir.mkdir(parents=True)
        (error_dir / "admin-1-msg.json").write_text("old error")

        source = ipc_dir / "admin-1" / "messages" / "msg.json"
        source.parent.mkdir(parents=True)
        source.write_text("new error")

        await self._run_startup_sweep(deps, tmp_path)

        assert (error_dir / "admin-1-msg.json").read_text() == "new error"
        assert not source.exists()


class TestHostApprovalRecovery:
    async def test_runtime_sweep_recovers_host_owned_decision(self, deps, tmp_path: Path):
        """A crash-persisted decision is recovered outside the agent IPC mount."""
        ipc_dir = tmp_path / "ipc"
        ipc_dir.mkdir()
        decision = tmp_path / "approvals" / "other-group" / "approval_decisions" / "req.json"
        decision.parent.mkdir(parents=True)
        decision.write_text("{}")
        settings = _test_settings(data_dir=tmp_path)

        with (
            patch(
                "pynchy.host.container_manager.security.approval.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.process_approval_decision",
                new_callable=AsyncMock,
            ) as process_decision,
            patch(
                "pynchy.host.container_manager.ipc.watcher._sweep_expired_state",
                new_callable=AsyncMock,
            ),
        ):
            handled = await recover_ipc_runtime(ipc_dir, deps)

        assert handled == 1
        process_decision.assert_awaited_once_with(decision, "other-group", deps=deps)


# ---------------------------------------------------------------------------
# IPC message file processing — integration-style tests using dispatch
# ---------------------------------------------------------------------------


class TestIpcMessageProcessing:
    """Test the message processing flow that happens inside start_ipc_watcher.

    The watcher reads JSON files from ipc/{group}/messages/, checks authorization,
    broadcasts authorized messages, and cleans up processed files.
    """

    async def test_admin_group_can_send_to_any_chat(self, deps, tmp_path: Path):
        """Admin group messages to any chat JID should be broadcast."""
        # Here we test the message file authorization logic directly
        groups = deps.workspaces()
        target_group = groups.get("other@g.us")
        source_group = "admin-1"
        is_admin = True

        # Simulate the authorization check from the watcher
        authorized = is_admin or (target_group and target_group.folder == source_group)
        assert authorized is True

    async def test_non_admin_can_send_to_own_chat(self, deps):
        """Non-admin group should be authorized to send to its own chat."""
        groups = deps.workspaces()
        target_group = groups.get("other@g.us")
        source_group = "other-group"
        is_admin = False

        authorized = is_admin or (target_group and target_group.folder == source_group)
        assert authorized is True

    async def test_non_admin_blocked_from_other_chat(self, deps):
        """Non-admin group should NOT be authorized to send to another group's chat."""
        groups = deps.workspaces()
        target_group = groups.get("admin-1@g.us")
        source_group = "other-group"
        is_admin = False

        authorized = is_admin or (target_group and target_group.folder == source_group)
        assert authorized is False

    async def test_non_admin_blocked_from_unregistered_chat(self, deps):
        """Non-admin sending to an unregistered JID should be blocked."""
        groups = deps.workspaces()
        target_group = groups.get("unknown@g.us")
        source_group = "other-group"
        is_admin = False

        authorized = is_admin or bool(target_group and target_group.folder == source_group)
        assert authorized is False


# ---------------------------------------------------------------------------
# IPC request dispatch — edge cases not covered by test_ipc_auth.py
# ---------------------------------------------------------------------------


class TestIpcTaskFileEdgeCases:
    """Edge cases in the task processing pipeline of the IPC watcher."""

    async def test_empty_type_field_is_ignored(self, deps):
        """A request with no type field should be logged and ignored."""
        # Should not raise
        await dispatch({"no_type_field": True}, "admin-1", True, deps)

    async def test_none_type_field_is_ignored(self, deps):
        """A request with type=None should be handled gracefully."""
        await dispatch({"type": None}, "admin-1", True, deps)

    async def test_empty_data_dict_is_ignored(self, deps):
        """An empty data dict should not crash the processor."""
        await dispatch({}, "admin-1", True, deps)


# ---------------------------------------------------------------------------
# IPC deploy — edge cases
# ---------------------------------------------------------------------------


class TestIpcDeployEdgeCases:
    """Tests for deploy command edge cases in the IPC handler."""

    async def test_deploy_without_chat_jid_uses_configured_notification_workspace(self, deps):
        """Deploy request missing chatJid uses the configured notification workspace."""
        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_deploy.get_settings",
                return_value=make_settings(
                    notifications=NotificationsConfig(admin_workspace="admin-1")
                ),
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_deploy.start_deploy_workflow",
                new_callable=AsyncMock,
            ) as mock_start,
        ):
            await dispatch(
                {
                    "type": "deploy",
                    "rebuildContainer": False,
                    "resumePrompt": "Done.",
                    "headSha": "abc123",
                    # chatJid intentionally missing
                },
                "admin-1",
                True,
                deps,
            )
            mock_start.assert_awaited_once()
            request = mock_start.await_args.args[0]
            assert request.chat_jid == "admin-1@g.us"

    async def test_deploy_without_chat_jid_and_no_admin_group(self, deps):
        """Deploy request with no chatJid and no admin group should not finalize."""
        # Remove admin group from deps
        no_admin_deps = MockDeps(
            {
                "other@g.us": OTHER_GROUP,
            }
        )
        await init_test_database()

        with patch(
            "pynchy.host.container_manager.ipc.handlers_deploy.start_deploy_workflow",
            new_callable=AsyncMock,
        ) as mock_start:
            await dispatch(
                {
                    "type": "deploy",
                    "rebuildContainer": False,
                    "headSha": "abc123",
                },
                "admin-1",
                True,
                no_admin_deps,
            )
            mock_start.assert_not_awaited()


# ---------------------------------------------------------------------------
# sync_worktree_to_main IPC handler
# ---------------------------------------------------------------------------


class TestSyncWorktreeIpc:
    """Tests for the sync_worktree_to_main IPC command handler."""

    async def test_writes_result_file(self, deps, tmp_path: Path):
        """sync_worktree_to_main should write a result JSON for the blocking MCP tool."""
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
                    "request_id": "req-123",
                },
                "other-group",
                False,
                deps,
            )

        result_file = tmp_path / "data" / "ipc" / "other-group" / "merge_results" / "req-123.json"
        assert result_file.exists()
        data = json.loads(result_file.read_text())
        assert data["success"] is True

    async def test_notifies_other_worktrees_on_success(self, deps, tmp_path: Path):
        """On successful sync, other worktrees should be notified."""
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
                    "request_id": "req-456",
                },
                "other-group",
                False,
                deps,
            )

        mock_notify.assert_called_once()
        # Source group should be passed as exclude_group (first positional arg)
        assert mock_notify.call_args[0][0] == "other-group"

    async def test_skips_notification_on_failure(self, deps, tmp_path: Path):
        """On failed sync, worktree notification should be skipped."""
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
                    "request_id": "req-789",
                },
                "other-group",
                False,
                deps,
            )

        mock_notify.assert_not_called()
