"""Tests for the watchdog-based IPC watcher.

Covers: startup sweep (crash recovery), signal-only IPC handling,
file processing helpers, and the IpcEventHandler filtering logic.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from conftest import make_settings

from pynchy.db import _init_test_database
from pynchy.ipc._watcher import (
    _handle_signal,
    _IpcEventHandler,
    _process_message_file,
    _process_task_file,
    _sweep_directory,
)
from pynchy.types import WorkspaceProfile

GOD_GROUP = WorkspaceProfile(
    jid="god@g.us",
    name="God",
    folder="god",
    trigger="always",
    added_at="2024-01-01",
    is_god=True,
)

OTHER_GROUP = WorkspaceProfile(
    jid="other@g.us",
    name="Other",
    folder="other-group",
    trigger="@pynchy",
    added_at="2024-01-01",
)


def _test_settings(*, data_dir: Path):
    return make_settings(data_dir=data_dir)


class MockDeps:
    """Mock IPC dependencies for watcher testing."""

    def __init__(self, groups: dict[str, WorkspaceProfile]):
        self._groups = groups
        self.broadcast_messages: list[tuple[str, str]] = []
        self.host_messages: list[tuple[str, str]] = []
        self.system_notices: list[tuple[str, str]] = []
        self.cleared_sessions: list[str] = []
        self.cleared_chats: list[str] = []
        self.enqueued_checks: list[str] = []
        self.sync_calls: list[bool] = []
        self.snapshot_calls: list[tuple] = []

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
        self.sync_calls.append(force)

    async def get_available_groups(self) -> list[Any]:
        return []

    def write_groups_snapshot(
        self,
        group_folder: str,
        is_god: bool,
        available_groups: list[Any],
        registered_jids: set[str],
    ) -> None:
        self.snapshot_calls.append((group_folder, is_god, available_groups, registered_jids))

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


@pytest.fixture
async def deps():
    await _init_test_database()
    return MockDeps(
        {
            "god@g.us": GOD_GROUP,
            "other@g.us": OTHER_GROUP,
        }
    )


def _write_ipc_file(base_dir: Path, group: str, subdir: str, data: dict) -> Path:
    """Helper to create an IPC file in the expected directory structure."""
    target_dir = base_dir / group / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    file_path = target_dir / "test.json"
    file_path.write_text(json.dumps(data))
    return file_path


# ---------------------------------------------------------------------------
# Startup sweep (crash recovery)
# ---------------------------------------------------------------------------


class TestStartupSweep:
    """Tests for _sweep_directory which processes files left over from crashes."""

    async def test_sweep_processes_message_files(self, deps, tmp_path: Path):
        """Startup sweep should process leftover message files."""
        ipc_dir = tmp_path / "ipc"
        _write_ipc_file(
            ipc_dir,
            "god",
            "messages",
            {"type": "message", "chatJid": "other@g.us", "text": "hello from sweep"},
        )

        with patch(
            "pynchy.ipc._watcher.get_settings",
            return_value=_test_settings(data_dir=tmp_path),
        ):
            processed = await _sweep_directory(ipc_dir, deps)

        assert processed == 1
        assert len(deps.broadcast_messages) == 1
        assert "hello from sweep" in deps.broadcast_messages[0][1]

    async def test_sweep_processes_task_files(self, deps, tmp_path: Path):
        """Startup sweep should process leftover task files."""
        ipc_dir = tmp_path / "ipc"
        _write_ipc_file(
            ipc_dir,
            "god",
            "tasks",
            {
                "type": "register_group",
                "jid": "new@g.us",
                "name": "New",
                "folder": "new",
                "trigger": "@pynchy",
            },
        )

        with patch(
            "pynchy.ipc._watcher.get_settings",
            return_value=_test_settings(data_dir=tmp_path),
        ):
            processed = await _sweep_directory(ipc_dir, deps)

        assert processed == 1
        assert "new@g.us" in deps.workspaces()

    async def test_sweep_handles_signal_files(self, deps, tmp_path: Path):
        """Startup sweep should process signal-format files."""
        ipc_dir = tmp_path / "ipc"
        _write_ipc_file(
            ipc_dir,
            "god",
            "tasks",
            {"signal": "refresh_groups"},
        )

        with patch(
            "pynchy.ipc._watcher.get_settings",
            return_value=_test_settings(data_dir=tmp_path),
        ):
            deps.sync_group_metadata = AsyncMock()
            deps.get_available_groups = AsyncMock(return_value=[])
            processed = await _sweep_directory(ipc_dir, deps)

        assert processed == 1
        deps.sync_group_metadata.assert_called_once_with(True)

    async def test_sweep_empty_directory_returns_zero(self, deps, tmp_path: Path):
        """Sweep of an empty IPC directory should return 0."""
        ipc_dir = tmp_path / "ipc"
        ipc_dir.mkdir()

        processed = await _sweep_directory(ipc_dir, deps)
        assert processed == 0

    async def test_sweep_skips_errors_directory(self, deps, tmp_path: Path):
        """Sweep should not process files in the errors/ directory."""
        ipc_dir = tmp_path / "ipc"
        error_dir = ipc_dir / "errors"
        error_dir.mkdir(parents=True)
        (error_dir / "test.json").write_text(json.dumps({"type": "bad"}))

        processed = await _sweep_directory(ipc_dir, deps)
        assert processed == 0

    async def test_sweep_cleans_up_processed_files(self, deps, tmp_path: Path):
        """Processed files should be removed after sweep."""
        ipc_dir = tmp_path / "ipc"
        file_path = _write_ipc_file(
            ipc_dir,
            "god",
            "messages",
            {"type": "message", "chatJid": "god@g.us", "text": "cleanup test"},
        )

        with patch(
            "pynchy.ipc._watcher.get_settings",
            return_value=_test_settings(data_dir=tmp_path),
        ):
            await _sweep_directory(ipc_dir, deps)

        assert not file_path.exists()

    async def test_sweep_moves_bad_files_to_errors(self, deps, tmp_path: Path):
        """Malformed files should be moved to errors/ during sweep."""
        ipc_dir = tmp_path / "ipc"
        target_dir = ipc_dir / "god" / "messages"
        target_dir.mkdir(parents=True)
        bad_file = target_dir / "bad.json"
        bad_file.write_text("not json {{{")

        with patch(
            "pynchy.ipc._watcher.get_settings",
            return_value=_test_settings(data_dir=tmp_path),
        ):
            await _sweep_directory(ipc_dir, deps)

        assert not bad_file.exists()
        assert (ipc_dir / "errors" / "god-bad.json").exists()


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------


class TestSignalHandling:
    """Tests for the _handle_signal dispatcher."""

    async def test_refresh_groups_signal_from_god(self, deps):
        """God group sending refresh_groups signal should trigger metadata sync."""
        deps.sync_group_metadata = AsyncMock()
        deps.get_available_groups = AsyncMock(return_value=[])

        await _handle_signal("refresh_groups", "god", True, deps)

        deps.sync_group_metadata.assert_called_once_with(True)
        assert len(deps.snapshot_calls) == 1

    async def test_refresh_groups_signal_blocked_for_non_god(self, deps):
        """Non-god groups should not be able to trigger refresh_groups."""
        deps.sync_group_metadata = AsyncMock()

        await _handle_signal("refresh_groups", "other-group", False, deps)

        deps.sync_group_metadata.assert_not_called()

    async def test_unknown_signal_is_logged(self, deps):
        """Unknown signals should be handled gracefully (logged but not crash)."""
        # This shouldn't raise — the watcher should log and continue
        await _handle_signal("unknown_future_signal", "god", True, deps)


# ---------------------------------------------------------------------------
# Task file processing — signal vs legacy dispatch
# ---------------------------------------------------------------------------


class TestTaskFileProcessing:
    """Tests for _process_task_file distinguishing signals from legacy tasks."""

    async def test_signal_file_is_handled_as_signal(self, deps, tmp_path: Path):
        """A file with signal format should be routed through _handle_signal."""
        ipc_dir = tmp_path / "ipc"
        file_path = _write_ipc_file(ipc_dir, "god", "tasks", {"signal": "refresh_groups"})

        deps.sync_group_metadata = AsyncMock()
        deps.get_available_groups = AsyncMock(return_value=[])

        with patch(
            "pynchy.ipc._watcher.get_settings",
            return_value=_test_settings(data_dir=tmp_path),
        ):
            await _process_task_file(file_path, "god", True, ipc_dir, deps)

        deps.sync_group_metadata.assert_called_once()
        assert not file_path.exists()  # File should be cleaned up

    async def test_legacy_task_file_uses_dispatch(self, deps, tmp_path: Path):
        """A file with legacy format should be routed through dispatch."""
        ipc_dir = tmp_path / "ipc"
        file_path = _write_ipc_file(
            ipc_dir,
            "god",
            "tasks",
            {
                "type": "register_group",
                "jid": "test@g.us",
                "name": "Test",
                "folder": "test",
                "trigger": "@p",
            },
        )

        await _process_task_file(file_path, "god", True, ipc_dir, deps)

        assert "test@g.us" in deps.workspaces()
        assert not file_path.exists()

    async def test_malformed_signal_goes_to_errors(self, deps, tmp_path: Path):
        """A file claiming to be a signal but with extra payload should error."""
        ipc_dir = tmp_path / "ipc"
        file_path = _write_ipc_file(
            ipc_dir,
            "god",
            "tasks",
            {"signal": "refresh_groups", "extra_payload": "bad"},
        )

        await _process_task_file(file_path, "god", True, ipc_dir, deps)

        # Should have been moved to errors
        assert not file_path.exists()
        assert (ipc_dir / "errors" / "god-test.json").exists()


# ---------------------------------------------------------------------------
# Message file processing
# ---------------------------------------------------------------------------


class TestMessageFileProcessing:
    """Tests for _process_message_file."""

    async def test_authorized_message_is_broadcast(self, deps, tmp_path: Path):
        """God group message to any chat should be broadcast."""
        ipc_dir = tmp_path / "ipc"
        file_path = _write_ipc_file(
            ipc_dir,
            "god",
            "messages",
            {"type": "message", "chatJid": "other@g.us", "text": "hello"},
        )

        with patch(
            "pynchy.ipc._watcher.get_settings",
            return_value=_test_settings(data_dir=tmp_path),
        ):
            await _process_message_file(file_path, "god", True, ipc_dir, deps)

        assert len(deps.broadcast_messages) == 1
        assert "hello" in deps.broadcast_messages[0][1]
        assert not file_path.exists()

    async def test_unauthorized_message_is_blocked(self, deps, tmp_path: Path):
        """Non-god group message to another group's chat should be blocked."""
        ipc_dir = tmp_path / "ipc"
        file_path = _write_ipc_file(
            ipc_dir,
            "other-group",
            "messages",
            {"type": "message", "chatJid": "god@g.us", "text": "sneaky"},
        )

        with patch(
            "pynchy.ipc._watcher.get_settings",
            return_value=_test_settings(data_dir=tmp_path),
        ):
            await _process_message_file(file_path, "other-group", False, ipc_dir, deps)

        assert len(deps.broadcast_messages) == 0
        # File should still be cleaned up (not retried)
        assert not file_path.exists()

    async def test_message_with_sender_uses_sender_prefix(self, deps, tmp_path: Path):
        """Messages with a sender field should use that as prefix."""
        ipc_dir = tmp_path / "ipc"
        file_path = _write_ipc_file(
            ipc_dir,
            "god",
            "messages",
            {"type": "message", "chatJid": "god@g.us", "text": "update", "sender": "Researcher"},
        )

        with patch(
            "pynchy.ipc._watcher.get_settings",
            return_value=_test_settings(data_dir=tmp_path),
        ):
            await _process_message_file(file_path, "god", True, ipc_dir, deps)

        assert deps.broadcast_messages[0][1] == "Researcher: update"

    async def test_malformed_json_goes_to_errors(self, deps, tmp_path: Path):
        """A file with invalid JSON should be moved to errors/."""
        ipc_dir = tmp_path / "ipc"
        target_dir = ipc_dir / "god" / "messages"
        target_dir.mkdir(parents=True)
        file_path = target_dir / "broken.json"
        file_path.write_text("not valid json")

        with patch(
            "pynchy.ipc._watcher.get_settings",
            return_value=_test_settings(data_dir=tmp_path),
        ):
            await _process_message_file(file_path, "god", True, ipc_dir, deps)

        assert not file_path.exists()
        assert (ipc_dir / "errors" / "god-broken.json").exists()


# ---------------------------------------------------------------------------
# IpcEventHandler — file event filtering
# ---------------------------------------------------------------------------


class TestIpcEventHandler:
    """Tests for the watchdog event handler filtering logic.

    Uses loop.run_until_complete() to drain call_soon_threadsafe callbacks
    that the handler schedules on the event loop.
    """

    @staticmethod
    def _drain(loop: asyncio.AbstractEventLoop) -> None:
        """Run the event loop briefly to execute pending callbacks."""
        loop.run_until_complete(asyncio.sleep(0))

    def test_only_json_files_are_queued(self):
        """Non-JSON files should be ignored."""
        loop = asyncio.new_event_loop()
        queue: asyncio.Queue[Path] = asyncio.Queue()
        ipc_dir = Path("/tmp/test-ipc")

        handler = _IpcEventHandler(ipc_dir, loop, queue)

        from watchdog.events import FileCreatedEvent

        # JSON file in expected path — should be queued
        handler.on_created(FileCreatedEvent(str(ipc_dir / "god" / "messages" / "test.json")))
        self._drain(loop)
        assert queue.qsize() == 1

        # Non-JSON file — should be ignored
        handler.on_created(FileCreatedEvent(str(ipc_dir / "god" / "messages" / "test.txt")))
        self._drain(loop)
        assert queue.qsize() == 1  # Still 1

        loop.close()

    def test_files_outside_group_subdirs_are_ignored(self):
        """Files not in a group's messages/ or tasks/ dir should be ignored."""
        loop = asyncio.new_event_loop()
        queue: asyncio.Queue[Path] = asyncio.Queue()
        ipc_dir = Path("/tmp/test-ipc")

        handler = _IpcEventHandler(ipc_dir, loop, queue)

        from watchdog.events import FileCreatedEvent

        # File directly in group dir (not messages/ or tasks/) — ignored
        handler.on_created(FileCreatedEvent(str(ipc_dir / "god" / "random.json")))
        self._drain(loop)
        assert queue.qsize() == 0

        # File in errors/ — ignored
        handler.on_created(FileCreatedEvent(str(ipc_dir / "errors" / "bad.json")))
        self._drain(loop)
        assert queue.qsize() == 0

        # File in tasks/ — should be queued
        handler.on_created(FileCreatedEvent(str(ipc_dir / "god" / "tasks" / "task.json")))
        self._drain(loop)
        assert queue.qsize() == 1

        loop.close()
