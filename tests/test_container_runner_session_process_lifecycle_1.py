"""Tests for the container runner."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.host.container_manager import session as session_mod
from pynchy.host.container_manager.session import RuntimeMonitorPolicy, SessionDiedError
from pynchy.workspace.api import (
    WorkspaceProfile,
)
from tests.container_runner_support import (
    FakeProcess,
    create_session,
)

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


class TestSessionProcessLifecycle:
    """Tests for observable process and runtime lifecycle outcomes."""

    @pytest.fixture(autouse=True)
    def _patch_container_record_cleanup(self):
        with (
            patch(
                "pynchy.host.container_manager.session.docker_rm_force",
                new_callable=AsyncMock,
            ) as cleanup,
        ):
            self.container_record_cleanup = cleanup
            yield cleanup

    async def test_start_rejects_a_process_without_stderr_pipe(self):
        session = session_mod.ContainerSession(
            "missing-stderr-test",
            "pynchy-missing-stderr-test",
            runtime_probe=AsyncMock(return_value=False),
        )
        proc = FakeProcess()
        proc.stderr = None

        with pytest.raises(RuntimeError, match="stderr pipe"):
            session.start(proc)  # type: ignore[arg-type]

    async def test_get_session_removes_a_dead_registered_session(self):
        proc = FakeProcess()
        session = await create_session(
            "dead-registered-test",
            "pynchy-dead-registered-test",
            proc,
            data_dir=Path("unused-data"),
            idle_timeout=0.0,
        )
        proc.close(code=1)
        with pytest.raises(SessionDiedError):
            await session.wait_for_query_done(query_timeout_seconds=0.2)

        assert session_mod.get_session("dead-registered-test") is None

    async def test_send_ipc_message_forwards_optional_routing_metadata(self):
        session = session_mod.ContainerSession(
            "ipc-message-test",
            "pynchy-ipc-message-test",
        )
        with patch("pynchy.host.container_manager.session.write_ipc_message") as write_message:
            await session.send_ipc_message(
                "follow-up",
                turn_id="turn-1",
                query_id="query-1",
                metadata={"source": "test"},
            )

        write_message.assert_called_once_with(
            "ipc-message-test",
            "follow-up",
            turn_id="turn-1",
            query_id="query-1",
            metadata={"source": "test"},
        )

    async def test_proc_monitor_detects_death_during_query(self):
        """A crash before a completion pulse should fail the active query."""
        session = session_mod.ContainerSession(
            "death-test",
            "pynchy-death-test",
            runtime_probe=AsyncMock(return_value=False),
        )
        proc = FakeProcess()

        session.start(proc)  # type: ignore[arg-type]
        session.set_output_handler(AsyncMock())
        proc.close(code=1)

        with pytest.raises(SessionDiedError):
            await session.wait_for_query_done(query_timeout_seconds=0.2)
        assert session.is_alive is False

    async def test_proc_monitor_removes_exited_container_record(self):
        """Exited containers should be removed even when no teardown command runs."""
        session = session_mod.ContainerSession(
            "record-cleanup-test",
            "pynchy-record-cleanup-test",
            runtime_probe=AsyncMock(return_value=False),
        )
        proc = FakeProcess()

        session.start(proc)  # type: ignore[arg-type]
        proc.close(code=0)
        await session.wait_for_query_done(query_timeout_seconds=0.2)
        await asyncio.sleep(0)

        self.container_record_cleanup.assert_awaited_once_with("pynchy-record-cleanup-test")

    async def test_proc_monitor_clean_exit_completes_query(self):
        """A clean exit before a pulse should not be reported as a crash."""
        session = session_mod.ContainerSession(
            "clean-exit-test",
            "pynchy-clean-exit-test",
            runtime_probe=AsyncMock(return_value=False),
        )
        proc = FakeProcess()

        session.start(proc)  # type: ignore[arg-type]

        session.set_output_handler(AsyncMock())
        proc.close(code=0)

        await session.wait_for_query_done(query_timeout_seconds=0.2)
        assert session.is_alive is False

    async def test_runtime_container_survives_cli_process_exit(self):
        """Apple Container can keep the container alive after the CLI process exits."""
        runtime_running = True

        runtime_probe = AsyncMock(side_effect=lambda _container_name: runtime_running)

        session = session_mod.ContainerSession(
            "apple-cli-test",
            "pynchy-apple-cli-test",
            runtime_probe=runtime_probe,
            runtime_monitor_policy=RuntimeMonitorPolicy(poll_interval_seconds=0.01),
        )
        proc = FakeProcess()
        with patch("pynchy.host.container_manager.session.sys.platform", "darwin"):
            session.start(proc)  # type: ignore[arg-type]
            session.set_output_handler(AsyncMock())

            proc.close(code=1)
            await asyncio.sleep(0.05)

            assert session.is_alive is True

            session.signal_query_done()
            runtime_running = False
            await asyncio.sleep(0.05)

        assert session.is_alive is False

    async def test_runtime_container_stop_unblocks_query_when_cli_process_hangs(self):
        """Apple Container can stop the container while the CLI process keeps hanging."""
        runtime_running = True

        runtime_probe = AsyncMock(side_effect=lambda _container_name: runtime_running)

        session = session_mod.ContainerSession(
            "apple-runtime-stop-test",
            "pynchy-apple-runtime-stop-test",
            runtime_probe=runtime_probe,
            runtime_monitor_policy=RuntimeMonitorPolicy(
                poll_interval_seconds=0.01,
                cli_kill_wait_seconds=0.01,
            ),
        )
        proc = FakeProcess()
        with (
            patch("pynchy.host.container_manager.session.sys.platform", "darwin"),
            patch(
                "pynchy.host.container_manager.session.reap_apple_runtime_orphans",
                new=AsyncMock(return_value=True),
            ) as reap_orphan,
        ):
            session.start(proc)  # type: ignore[arg-type]
            session.set_output_handler(AsyncMock())

            await asyncio.sleep(0.02)
            runtime_running = False

            with pytest.raises(SessionDiedError):
                await session.wait_for_query_done(query_timeout_seconds=0.5)

        assert proc._killed is True
        reap_orphan.assert_awaited_once_with("pynchy-apple-runtime-stop-test")
        assert session.is_alive is False

    async def test_runtime_container_never_starts_unblocks_query_when_cli_process_hangs(self):
        """Apple Container can leave ``container run`` alive after startup failure."""

        runtime_probe = AsyncMock(return_value=False)

        session = session_mod.ContainerSession(
            "apple-runtime-never-start-test",
            "pynchy-never-start",
            runtime_probe=runtime_probe,
            runtime_monitor_policy=RuntimeMonitorPolicy(
                poll_interval_seconds=0.01,
                start_grace_seconds=0.02,
                cli_kill_wait_seconds=0.01,
            ),
        )
        proc = FakeProcess()
        with (
            patch("pynchy.host.container_manager.session.sys.platform", "darwin"),
            patch(
                "pynchy.host.container_manager.session.reap_apple_runtime_orphans",
                new=AsyncMock(return_value=True),
            ) as reap_orphan,
        ):
            session.start(proc)  # type: ignore[arg-type]
            session.set_output_handler(AsyncMock())

            with pytest.raises(SessionDiedError):
                await session.wait_for_query_done(query_timeout_seconds=0.5)

        assert proc._killed is True
        reap_orphan.assert_awaited_once_with("pynchy-never-start")
        assert session.is_alive is False
