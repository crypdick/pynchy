"""Behavioral coverage for IPC watcher recovery and ownership edges."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from conftest_helpers import NullIpcDeps

from pynchy.host.container_manager.ipc.watcher import (
    process_ipc_message_file,
    process_ipc_request_file,
    recover_ipc_runtime,
    recover_ipc_startup,
    start_ipc_watcher,
)


@pytest.mark.parametrize("recover", [recover_ipc_startup, recover_ipc_runtime])
async def test_recovery_returns_zero_when_ipc_root_cannot_be_read(recover, tmp_path: Path):
    with (
        patch(
            "pynchy.host.container_manager.ipc.watcher._group_folders_in_ipc_dir",
            side_effect=OSError("unreadable"),
        ),
        patch(
            "pynchy.host.container_manager.ipc.watcher._sweep_expired_state",
            new_callable=AsyncMock,
        ),
        patch(
            "pynchy.host.container_manager.ipc.watcher.sweep_host_approval_decisions",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        assert await recover(tmp_path / "ipc", NullIpcDeps()) == 0


@pytest.mark.parametrize("processor", [process_ipc_message_file, process_ipc_request_file])
async def test_processing_drops_claimed_file_when_another_observer_owns_it(
    processor,
    tmp_path: Path,
):
    file_path = tmp_path / "request.json"
    file_path.write_text("{}")
    with (
        patch(
            "pynchy.host.container_manager.ipc.watcher.claim_ipc_file",
            return_value=False,
        ),
        patch(
            "pynchy.host.container_manager.ipc.watcher.handle_message_file",
            new_callable=AsyncMock,
        ) as handle_message,
        patch(
            "pynchy.host.container_manager.ipc.watcher._handle_request_file",
            new_callable=AsyncMock,
        ) as handle_request,
    ):
        await processor(
            file_path,
            "group",
            is_admin=False,
            ipc_base_dir=tmp_path,
            deps=NullIpcDeps(),
        )

    handle_message.assert_not_awaited()
    handle_request.assert_not_awaited()
    assert file_path.exists()


async def test_message_error_is_safe_when_error_directory_move_also_fails(tmp_path: Path):
    file_path = tmp_path / "messages" / "broken.json"
    file_path.parent.mkdir()
    file_path.write_text("broken")

    def fail_rename(_self: Path, _target: Path) -> Path:
        raise OSError("rename failed")

    with (
        patch(
            "pynchy.host.container_manager.ipc.watcher.handle_message_file",
            new_callable=AsyncMock,
            side_effect=RuntimeError("handler failed"),
        ),
        patch.object(Path, "rename", fail_rename),
    ):
        await process_ipc_message_file(
            file_path,
            "group",
            is_admin=False,
            ipc_base_dir=tmp_path,
            deps=NullIpcDeps(),
        )

    assert not file_path.exists()


@pytest.mark.parametrize("recover", [recover_ipc_startup, recover_ipc_runtime])
async def test_recovery_ignores_unreadable_group_queue(recover, tmp_path: Path):
    ipc_dir = tmp_path / "ipc"
    (ipc_dir / "group").mkdir(parents=True)

    with (
        patch(
            "pynchy.host.container_manager.ipc.watcher._json_files_in_dir",
            side_effect=OSError("unreadable"),
        ),
        patch(
            "pynchy.host.container_manager.ipc.watcher._sweep_expired_state",
            new_callable=AsyncMock,
        ),
        patch(
            "pynchy.host.container_manager.ipc.watcher.sweep_host_approval_decisions",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        assert await recover(ipc_dir, NullIpcDeps()) == 0


async def test_startup_cleanup_keeps_output_when_delete_fails(tmp_path: Path):
    ipc_dir = tmp_path / "ipc"
    output_file = ipc_dir / "group" / "output" / "stale.json"
    output_file.parent.mkdir(parents=True)
    output_file.write_text("stale")
    original_unlink = Path.unlink

    def fail_output_unlink(self: Path, *, missing_ok: bool = False) -> None:
        if self == output_file:
            raise OSError("busy")
        original_unlink(self, missing_ok=missing_ok)

    with patch.object(Path, "unlink", fail_output_unlink):
        assert await recover_ipc_startup(ipc_dir, NullIpcDeps()) == 0

    assert output_file.exists()


async def test_startup_cleanup_keeps_initial_file_when_delete_fails(tmp_path: Path):
    ipc_dir = tmp_path / "ipc"
    initial_file = ipc_dir / "group" / "input" / "initial.json"
    initial_file.parent.mkdir(parents=True)
    initial_file.write_text("initial")
    original_unlink = Path.unlink

    def fail_initial_unlink(self: Path, *, missing_ok: bool = False) -> None:
        if self == initial_file:
            raise OSError("busy")
        original_unlink(self, missing_ok=missing_ok)

    with patch.object(Path, "unlink", fail_initial_unlink):
        assert await recover_ipc_startup(ipc_dir, NullIpcDeps()) == 0

    assert initial_file.exists()


class _ExpiryDeps(NullIpcDeps):
    def __init__(self) -> None:
        self.expire_action_intent = AsyncMock()
        self.sweep_expired_questions = AsyncMock(side_effect=self._sweep_questions)

    async def _sweep_questions(self, write_response) -> list[dict[str, str]]:
        write_response("group", "question-1", "expired")
        return [{"request_id": "question-1"}]


async def test_startup_sweep_reports_expired_approvals_and_questions(tmp_path: Path):
    deps = _ExpiryDeps()
    ipc_dir = tmp_path / "ipc"
    ipc_dir.mkdir()
    response_path = tmp_path / "response.json"
    response_writer = Mock()

    with (
        patch(
            "pynchy.host.container_manager.security.approval.sweep_expired_approvals",
            new_callable=AsyncMock,
            return_value=[{"request_id": "approval-1"}],
        ) as sweep_approvals,
        patch(
            "pynchy.host.container_manager.ipc.watcher.ipc_response_path",
            return_value=response_path,
        ),
        patch(
            "pynchy.host.container_manager.ipc.watcher.write_ipc_response",
            response_writer,
        ),
    ):
        assert await recover_ipc_startup(ipc_dir, deps) == 0

    sweep_approvals.assert_awaited_once_with(deps.expire_action_intent)
    response_writer.assert_called_once_with(response_path, {"error": "expired"})


@dataclass
class _RunningState:
    running: bool = True
    runtime_sweep_task: object | None = None


async def test_start_is_noop_when_watcher_is_already_running(tmp_path: Path):
    with patch(
        "pynchy.host.container_manager.ipc.watcher._state",
        _RunningState(),
    ):
        await start_ipc_watcher(NullIpcDeps(), ipc_base_dir=tmp_path / "ipc")
