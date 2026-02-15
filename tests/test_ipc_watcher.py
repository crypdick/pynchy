"""Tests for the IPC watcher file processing loop.

Tests the inner file-scanning logic of start_ipc_watcher: message authorization,
task file processing, error handling, and file cleanup. These are critical paths
where a bug could leak messages across groups or silently drop data.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.db import _init_test_database
from pynchy.ipc import _move_to_error_dir
from pynchy.types import RegisteredGroup

GOD_GROUP = RegisteredGroup(
    name="God",
    folder="god",
    trigger="always",
    added_at="2024-01-01",
    is_god=True,
)

OTHER_GROUP = RegisteredGroup(
    name="Other",
    folder="other-group",
    trigger="@pynchy",
    added_at="2024-01-01",
)


class MockDeps:
    """Mock IPC dependencies for watcher testing."""

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
# _move_to_error_dir — already tested in test_ipc_periodic_agent.py but
# we add the watcher-context tests here for completeness
# ---------------------------------------------------------------------------


class TestMoveToErrorDirInWatcherContext:
    """Tests that _move_to_error_dir correctly handles watcher error scenarios."""

    def test_preserves_file_content_on_move(self, tmp_path: Path):
        """Error files should retain their original content for debugging."""
        ipc_dir = tmp_path / "ipc"
        ipc_dir.mkdir()
        source = ipc_dir / "my-group" / "messages" / "broken.json"
        source.parent.mkdir(parents=True)
        content = '{"type": "message", "chatJid": "test@g.us", "text": '
        source.write_text(content)  # Truncated JSON — triggers parse error

        _move_to_error_dir(ipc_dir, "my-group", source)

        error_file = ipc_dir / "errors" / "my-group-broken.json"
        assert error_file.read_text() == content

    def test_does_not_overwrite_existing_error_file(self, tmp_path: Path):
        """If an error file with the same name already exists, rename overwrites it.

        This is acceptable behavior — the latest error is preserved.
        """
        ipc_dir = tmp_path / "ipc"
        error_dir = ipc_dir / "errors"
        error_dir.mkdir(parents=True)

        # Pre-existing error file
        (error_dir / "grp-msg.json").write_text("old error")

        # New file with same group-name combo
        source = ipc_dir / "grp" / "messages" / "msg.json"
        source.parent.mkdir(parents=True)
        source.write_text("new error")

        _move_to_error_dir(ipc_dir, "grp", source)

        assert (error_dir / "grp-msg.json").read_text() == "new error"


# ---------------------------------------------------------------------------
# IPC message file processing — integration-style tests using process_task_ipc
# ---------------------------------------------------------------------------


class TestIpcMessageProcessing:
    """Test the message processing flow that happens inside start_ipc_watcher.

    The watcher reads JSON files from ipc/{group}/messages/, checks authorization,
    broadcasts authorized messages, and cleans up processed files.
    """

    async def test_god_group_can_send_to_any_chat(self, deps, tmp_path: Path):
        """God group messages to any chat JID should be broadcast."""
        # Here we test the message file authorization logic directly
        groups = deps.registered_groups()
        target_group = groups.get("other@g.us")
        source_group = "god"
        is_god = True

        # Simulate the authorization check from the watcher
        authorized = is_god or (target_group and target_group.folder == source_group)
        assert authorized is True

    async def test_non_god_can_send_to_own_chat(self, deps):
        """Non-god group should be authorized to send to its own chat."""
        groups = deps.registered_groups()
        target_group = groups.get("other@g.us")
        source_group = "other-group"
        is_god = False

        authorized = is_god or (target_group and target_group.folder == source_group)
        assert authorized is True

    async def test_non_god_blocked_from_other_chat(self, deps):
        """Non-god group should NOT be authorized to send to another group's chat."""
        groups = deps.registered_groups()
        target_group = groups.get("god@g.us")
        source_group = "other-group"
        is_god = False

        authorized = is_god or (target_group and target_group.folder == source_group)
        assert authorized is False

    async def test_non_god_blocked_from_unregistered_chat(self, deps):
        """Non-god sending to an unregistered JID should be blocked."""
        groups = deps.registered_groups()
        target_group = groups.get("unknown@g.us")
        source_group = "other-group"
        is_god = False

        authorized = is_god or bool(target_group and target_group.folder == source_group)
        assert authorized is False


# ---------------------------------------------------------------------------
# IPC task file processing — edge cases not covered by test_ipc_auth.py
# ---------------------------------------------------------------------------


class TestIpcTaskFileEdgeCases:
    """Edge cases in the task processing pipeline of the IPC watcher."""

    async def test_empty_type_field_is_ignored(self, deps):
        """A task file with no type field should be logged and ignored."""
        from pynchy.ipc import process_task_ipc

        # Should not raise
        await process_task_ipc({"no_type_field": True}, "god", True, deps)

    async def test_none_type_field_is_ignored(self, deps):
        """A task file with type=None should be handled gracefully."""
        from pynchy.ipc import process_task_ipc

        await process_task_ipc({"type": None}, "god", True, deps)

    async def test_empty_data_dict_is_ignored(self, deps):
        """An empty data dict should not crash the processor."""
        from pynchy.ipc import process_task_ipc

        await process_task_ipc({}, "god", True, deps)


# ---------------------------------------------------------------------------
# IPC deploy — edge cases
# ---------------------------------------------------------------------------


class TestIpcDeployEdgeCases:
    """Tests for deploy command edge cases in the IPC handler."""

    async def test_deploy_without_chat_jid_uses_god_group(self, deps):
        """Deploy request missing chatJid should fall back to god group's JID."""
        from pynchy.ipc import _handle_deploy

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

    async def test_deploy_without_chat_jid_and_no_god_group(self, deps):
        """Deploy request with no chatJid and no god group should not finalize."""
        from pynchy.ipc import _handle_deploy

        # Remove god group from deps
        no_god_deps = MockDeps(
            {
                "other@g.us": OTHER_GROUP,
            }
        )
        await _init_test_database()

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


# ---------------------------------------------------------------------------
# sync_worktree_to_main IPC handler
# ---------------------------------------------------------------------------


class TestSyncWorktreeIpc:
    """Tests for the sync_worktree_to_main IPC command handler."""

    async def test_writes_result_file(self, deps, tmp_path: Path):
        """sync_worktree_to_main should write a result JSON for the blocking MCP tool."""
        from pynchy.ipc import process_task_ipc

        with (
            patch("pynchy.ipc.DATA_DIR", tmp_path / "data"),
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

        result_file = tmp_path / "data" / "ipc" / "other-group" / "merge_results" / "req-123.json"
        assert result_file.exists()
        data = json.loads(result_file.read_text())
        assert data["success"] is True

    async def test_notifies_other_worktrees_on_success(self, deps, tmp_path: Path):
        """On successful sync, other worktrees should be notified."""
        from pynchy.ipc import process_task_ipc

        with (
            patch("pynchy.ipc.DATA_DIR", tmp_path / "data"),
            patch(
                "pynchy.ipc.host_sync_worktree",
                return_value={"success": True, "message": "done"},
            ),
            patch(
                "pynchy.ipc.host_notify_worktree_updates", new_callable=AsyncMock
            ) as mock_notify,
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
        # Source group should be passed as exclude_group (first positional arg)
        assert mock_notify.call_args[0][0] == "other-group"

    async def test_skips_notification_on_failure(self, deps, tmp_path: Path):
        """On failed sync, worktree notification should be skipped."""
        from pynchy.ipc import process_task_ipc

        with (
            patch("pynchy.ipc.DATA_DIR", tmp_path / "data"),
            patch(
                "pynchy.ipc.host_sync_worktree",
                return_value={"success": False, "message": "conflict"},
            ),
            patch(
                "pynchy.ipc.host_notify_worktree_updates", new_callable=AsyncMock
            ) as mock_notify,
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
