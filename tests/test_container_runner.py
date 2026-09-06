"""Tests for the container runner."""

from __future__ import annotations

import asyncio
import json
import signal
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

from conftest import (
    make_settings,
)

from pynchy.agent_protocol.api import (
    ContainerInput,
    input_to_dict,
    parse_container_output,
)
from pynchy.host.container_manager import process as process_mod
from pynchy.host.container_manager.ipc.write import clean_ipc_input_dir
from pynchy.host.container_manager.orchestrator import write_initial_input
from pynchy.workspace.api import (
    WorkspaceProfile,
)
from tests.container_runner_support import (
    CompletedProcess,
    DelayedExitProcess,
    FakeProcess,
    KillableHangingProcess,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_GROUP = WorkspaceProfile(
    jid="test@g.us",
    name="Test Group",
    folder="test-group",
    trigger="@pynchy",
    added_at="2024-01-01T00:00:00.000Z",
)

_CR_CREDS = "pynchy.host.container_manager.credentials"
_CR_ORCH = "pynchy.host.container_manager.orchestrator"
_GATEWAY = "pynchy.host.container_manager.gateway"


_SETTINGS_MODULES = [
    "pynchy.host.orchestrator.workspace_config",
]

_test_settings: ContextVar[Any | None] = ContextVar("test_settings", default=None)


class TestContainerProcessHelpers:
    async def test_delayed_stop_cli_is_retained_until_reaped(self):
        """A stop child exiting after the kill bound remains explicitly owned."""
        container_proc = FakeProcess()
        container_proc.close()
        stop_proc = DelayedExitProcess()

        with (
            patch(
                "pynchy.host.container_manager.process._runtime.container_cli",
                "container",
            ),
            patch(
                "pynchy.host.container_manager.process.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=stop_proc),
            ),
        ):
            await process_mod.graceful_stop(container_proc, "pynchy-delayed-stop")

        pending = {
            task
            for task in asyncio.all_tasks()
            if task.get_name() == "reap-container-stop-pynchy-delayed-stop"
        }
        assert {task.get_name() for task in pending} == {"reap-container-stop-pynchy-delayed-stop"}
        stop_proc.release()
        await asyncio.gather(*pending)
        await asyncio.sleep(0)
        assert all(
            task.get_name() != "reap-container-stop-pynchy-delayed-stop"
            for task in asyncio.all_tasks()
        )

    async def test_delayed_remove_cli_is_retained_until_reaped(self):
        """A remove child exiting after the kill bound remains explicitly owned."""
        remove_proc = DelayedExitProcess()

        with (
            patch(
                "pynchy.host.container_manager.process._runtime.container_cli",
                "container",
            ),
            patch(
                "pynchy.host.container_manager.process.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=remove_proc),
            ),
            patch(
                "pynchy.host.container_manager.process.reap_apple_runtime_orphans",
                new=AsyncMock(return_value=False),
            ),
        ):
            await process_mod.docker_rm_force(
                "pynchy-delayed-remove",
                timeout_seconds=0.01,
                retry_timeout_seconds=0.01,
            )

        pending = {
            task
            for task in asyncio.all_tasks()
            if task.get_name() == "reap-container-remove-pynchy-delayed-remove"
        }
        assert {task.get_name() for task in pending} == {
            "reap-container-remove-pynchy-delayed-remove"
        }
        remove_proc.release()
        await asyncio.gather(*pending)
        await asyncio.sleep(0)
        assert all(
            task.get_name() != "reap-container-remove-pynchy-delayed-remove"
            for task in asyncio.all_tasks()
        )

    async def test_graceful_stop_kills_and_reaps_timed_out_stop_cli(self):
        """A wedged management CLI must not outlive graceful container cleanup."""
        container_proc = KillableHangingProcess()
        stop_proc = KillableHangingProcess(timeout_first_wait=True)

        with (
            patch(
                "pynchy.host.container_manager.process._runtime.container_cli",
                "container",
            ),
            patch(
                "pynchy.host.container_manager.process.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=stop_proc),
            ),
        ):
            await process_mod.graceful_stop(container_proc, "pynchy-code-improver")

        assert stop_proc.killed is True
        assert stop_proc.wait_calls == 2
        assert container_proc.killed is True

    async def test_reap_apple_runtime_orphans_signals_exact_runtime_match(self):
        """Only the runtime process for the exact Apple container UUID is reaped."""
        container_root = "/Users/me/Library/Application Support/com.apple.container/containers"
        old_container = f"{container_root}/pynchy-code-improver-old"
        ps_output = (
            b"123 /opt/homebrew/bin/container-runtime-linux start --root "
            + f"{container_root}/pynchy-code-improver --uuid pynchy-code-improver\n".encode()
            + b"456 /opt/homebrew/bin/container-runtime-linux start --root "
            + f"{old_container} --uuid pynchy-code-improver-old\n".encode()
            + b"789 /usr/bin/other --uuid pynchy-code-improver\n"
        )
        alive = {123}
        signals: list[tuple[int, signal.Signals]] = []

        def fake_kill(pid: int, sig: signal.Signals) -> None:
            if sig == 0:
                if pid in alive:
                    return
                raise ProcessLookupError
            signals.append((pid, sig))
            if sig == signal.SIGTERM:
                alive.discard(pid)

        with (
            patch("pynchy.host.container_manager.process._runtime.is_apple_runtime", True),
            patch(
                "pynchy.host.container_manager.process.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=CompletedProcess(ps_output)),
            ),
            patch("pynchy.host.container_manager.process.os.kill", side_effect=fake_kill),
        ):
            reaped = await process_mod.reap_apple_runtime_orphans("pynchy-code-improver")

        assert reaped is True
        assert signals == [(123, signal.SIGTERM)]

    async def test_force_remove_times_out_and_kills_hung_runtime_cli(self):
        """Apple Container cleanup can hang on stopped containers with orphaned runtimes."""
        proc = KillableHangingProcess(timeout_first_wait=True)

        with (
            patch(
                "pynchy.host.container_manager.process._runtime.container_cli",
                "container",
            ),
            patch(
                "pynchy.host.container_manager.process.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=proc),
            ) as create_proc,
        ):
            await process_mod.docker_rm_force(
                "pynchy-code-improver", timeout_seconds=0.01, retry_timeout_seconds=0.01
            )

        assert proc.killed is True
        assert proc.wait_calls == 2
        create_proc.assert_awaited_once_with(
            "container",
            "rm",
            "-f",
            "pynchy-code-improver",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def test_force_remove_reaps_apple_runtime_orphan_after_quick_return(self):
        """Apple delete can return while the stopped container runtime remains alive."""
        completed_delete = FakeProcess()
        completed_delete.close(code=1)
        retry_delete = FakeProcess()
        retry_delete.close(code=1)

        with (
            patch(
                "pynchy.host.container_manager.process.asyncio.create_subprocess_exec",
                new=AsyncMock(side_effect=[completed_delete, retry_delete]),
            ) as create_proc,
            patch(
                "pynchy.host.container_manager.process.reap_apple_runtime_orphans",
                new=AsyncMock(return_value=True),
            ) as reap_orphan,
        ):
            await process_mod.docker_rm_force("pynchy-code-improver")

        reap_orphan.assert_awaited_once_with("pynchy-code-improver")
        assert create_proc.await_count == 2

    async def test_force_remove_retries_after_reaping_apple_runtime_orphan(self):
        """If Apple delete hangs, reap the orphaned runtime and retry cleanup once."""
        hung_delete = KillableHangingProcess(timeout_first_wait=True)
        completed_delete = FakeProcess()
        completed_delete.close(code=1)

        with (
            patch(
                "pynchy.host.container_manager.process.asyncio.create_subprocess_exec",
                new=AsyncMock(side_effect=[hung_delete, completed_delete]),
            ) as create_proc,
            patch(
                "pynchy.host.container_manager.process.reap_apple_runtime_orphans",
                new=AsyncMock(return_value=True),
            ) as reap_orphan,
        ):
            await process_mod.docker_rm_force(
                "pynchy-code-improver", timeout_seconds=0.01, retry_timeout_seconds=0.01
            )

        assert hung_delete.killed is True
        reap_orphan.assert_awaited_once_with("pynchy-code-improver")
        assert create_proc.await_count == 2


class TestInputSerialization:
    def test_basic_fields_snake_case(self):
        inp = ContainerInput(
            messages=[{"message_type": "user", "content": "hi"}],
            group_folder="my-group",
            chat_jid="chat@g.us",
            is_admin=True,
        )
        d = input_to_dict(inp)
        assert d == {
            "messages": [{"message_type": "user", "content": "hi"}],
            "group_folder": "my-group",
            "chat_jid": "chat@g.us",
            "is_admin": True,
            "is_scheduled_task": False,
            "input_source": "user",
            "corruption_tainted": False,
            "secret_tainted": False,
            "invocation_ts": 0.0,
            "agent_core_module": "agent_runner.cores.openai",
            "agent_core_class": "OpenAIAgentCore",
        }

    def test_optional_fields_included_when_set(self):
        inp = ContainerInput(
            messages=[{"message_type": "user", "content": "hi"}],
            group_folder="g",
            chat_jid="c",
            is_admin=False,
            session_id="sess-1",
            is_scheduled_task=True,
        )
        d = input_to_dict(inp)
        assert d["session_id"] == "sess-1"
        assert d["is_scheduled_task"] is True

    def test_optional_fields_omitted_when_none(self):
        inp = ContainerInput(
            messages=[{"message_type": "user", "content": "hi"}],
            group_folder="g",
            chat_jid="c",
            is_admin=False,
        )
        d = input_to_dict(inp)
        assert "session_id" not in d  # None → omitted
        assert d["is_scheduled_task"] is False  # False → included (non-None)


class TestWriteInitialInput:
    """Tests for write_initial_input — atomic file write of ContainerInput."""

    def test_creates_initial_json_with_correct_content(self, tmp_path: Path):
        inp = ContainerInput(
            messages=[{"message_type": "user", "content": "hello"}],
            group_folder="test-group",
            chat_jid="chat@g.us",
            is_admin=False,
        )
        input_dir = tmp_path / "ipc" / "test-group" / "input"
        write_initial_input(inp, input_dir)

        filepath = input_dir / "initial.json"
        assert filepath.exists()
        data = json.loads(filepath.read_text())
        assert data["messages"] == [{"message_type": "user", "content": "hello"}]
        assert data["group_folder"] == "test-group"
        assert data["chat_jid"] == "chat@g.us"
        assert data["is_admin"] is False

    def test_creates_parent_directories(self, tmp_path: Path):
        inp = ContainerInput(
            messages=[],
            group_folder="deep-group",
            chat_jid="c",
            is_admin=False,
        )
        input_dir = tmp_path / "a" / "b" / "c" / "input"
        assert not input_dir.exists()
        write_initial_input(inp, input_dir)
        assert (input_dir / "initial.json").exists()

    def test_atomic_write_no_tmp_left_behind(self, tmp_path: Path):
        inp = ContainerInput(
            messages=[{"message_type": "user", "content": "hi"}],
            group_folder="g",
            chat_jid="c",
            is_admin=False,
        )
        input_dir = tmp_path / "input"
        write_initial_input(inp, input_dir)

        # Only initial.json should exist — no .tmp file
        files = list(input_dir.iterdir())
        assert len(files) == 1
        assert files[0].name == "initial.json"

    def test_overwrites_existing_initial_json(self, tmp_path: Path):
        """Verify idempotency — a second call replaces the first file."""
        input_dir = tmp_path / "input"
        inp1 = ContainerInput(
            messages=[{"message_type": "user", "content": "first"}],
            group_folder="g",
            chat_jid="c",
            is_admin=False,
        )
        inp2 = ContainerInput(
            messages=[{"message_type": "user", "content": "second"}],
            group_folder="g",
            chat_jid="c",
            is_admin=True,
        )
        write_initial_input(inp1, input_dir)
        write_initial_input(inp2, input_dir)

        data = json.loads((input_dir / "initial.json").read_text())
        assert data["messages"][0]["content"] == "second"
        assert data["is_admin"] is True

    def test_optional_fields_round_trip(self, tmp_path: Path):
        """Optional fields like session_id survive the write/read cycle."""
        inp = ContainerInput(
            messages=[],
            group_folder="g",
            chat_jid="c",
            is_admin=False,
            session_id="sess-42",
            is_scheduled_task=True,
            system_notices=["notice1"],
        )
        input_dir = tmp_path / "input"
        write_initial_input(inp, input_dir)

        data = json.loads((input_dir / "initial.json").read_text())
        assert data["session_id"] == "sess-42"
        assert data["is_scheduled_task"] is True
        assert data["system_notices"] == ["notice1"]


class TestCleanIpcInputDir:
    """IPC cleanup removes all input belonging to the retired worker."""

    def test_deletes_all_worker_input(self, tmp_path: Path) -> None:
        settings = make_settings(data_dir=tmp_path)
        input_dir = tmp_path / "ipc" / "test-group" / "input"
        input_dir.mkdir(parents=True)
        (input_dir / "initial.json").write_text('{"messages": []}')
        (input_dir / "stale-msg.json").write_text('{"type": "message"}')
        (input_dir / "_close").write_text("")

        with patch(
            "pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"
        ):
            clean_ipc_input_dir("test-group")

        assert not (input_dir / "initial.json").exists()
        assert not (input_dir / "stale-msg.json").exists()
        assert not (input_dir / "_close").exists()

    def test_noop_for_none_group(self) -> None:
        """Should not raise when group_folder is None."""
        clean_ipc_input_dir(None)


class TestOutputParsing:
    def test_parses_snake_case_json(self):
        out = parse_container_output(
            json.dumps(
                {
                    "status": "success",
                    "result": "done",
                    "new_session_id": "s1",
                }
            )
        )
        assert out.status == "success"
        assert out.result == "done"
        assert out.new_session_id == "s1"

    def test_parses_error_output(self):
        out = parse_container_output(json.dumps({"status": "error", "error": "boom"}))
        assert out.status == "error"
        assert out.error == "boom"
        assert out.result is None
