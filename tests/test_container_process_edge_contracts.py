"""Behavioral coverage for bounded container-process fallbacks."""

from __future__ import annotations

import asyncio
import signal
from unittest.mock import AsyncMock, MagicMock, patch

from pynchy.host.container_manager import process
from tests.container_runner_support import (
    CompletedProcess,
    FakeProcess,
    KillableHangingProcess,
)


async def test_apple_runtime_probe_degrades_to_not_running_on_probe_failure():
    def fail_probe(_container_name: str) -> bool:
        raise OSError("runtime unavailable")

    process.configure_container_process_runtime(
        container_cli="docker",
        is_apple_runtime=True,
        container_is_running=fail_probe,
    )

    assert await process.runtime_container_running("worker") is False


async def test_graceful_stop_force_kills_when_stop_sequence_fails():
    container = FakeProcess()

    with (
        patch(
            "pynchy.host.container_manager.process._stop_container_process",
            new_callable=AsyncMock,
            side_effect=RuntimeError("stop failed"),
        ),
        patch.object(container, "kill", wraps=container.kill) as kill,
    ):
        await process.graceful_stop(container, "worker")

    kill.assert_called_once_with()


async def test_graceful_stop_kills_container_after_stop_cli_returns_but_container_hangs():
    stop_cli = FakeProcess()
    stop_cli.close()
    container = KillableHangingProcess(timeout_first_wait=True)

    with (
        patch(
            "pynchy.host.container_manager.process._runtime.container_cli",
            "container",
        ),
        patch(
            "pynchy.host.container_manager.process.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=stop_cli),
        ),
    ):
        await process.graceful_stop(container, "worker")

    assert container.killed is True
    assert container.wait_calls == 2


async def test_graceful_stop_force_kills_when_runtime_cli_is_not_configured():
    container = FakeProcess()
    with (
        patch(
            "pynchy.host.container_manager.process._runtime.container_cli",
            None,
        ),
        patch.object(container, "kill", wraps=container.kill) as kill,
    ):
        await process.graceful_stop(container, "worker")

    kill.assert_called_once_with()


async def test_runtime_probe_checks_non_apple_runtime():
    probe = MagicMock(return_value=True)
    process.configure_container_process_runtime(
        container_cli="docker",
        is_apple_runtime=False,
        container_is_running=probe,
    )

    assert await process.runtime_container_running("worker") is True
    probe.assert_called_once_with("worker")


async def test_runtime_probe_without_configured_probe_is_not_running():
    process.configure_container_process_runtime(
        container_cli="docker",
        is_apple_runtime=False,
        container_is_running=None,
    )

    assert await process.runtime_container_running("worker") is False


async def test_orphan_scan_degrades_when_process_listing_cannot_start():
    with (
        patch("pynchy.host.container_manager.process._runtime.is_apple_runtime", True),
        patch(
            "pynchy.host.container_manager.process.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=OSError("ps unavailable")),
        ),
    ):
        assert await process.reap_apple_runtime_orphans("worker") is False


async def test_orphan_scan_ignores_malformed_and_non_runtime_processes():
    ps_output = b"not-a-pid command\n123 /usr/bin/other --uuid worker\n"
    with (
        patch("pynchy.host.container_manager.process._runtime.is_apple_runtime", True),
        patch(
            "pynchy.host.container_manager.process.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=CompletedProcess(ps_output)),
        ),
    ):
        assert await process.reap_apple_runtime_orphans("worker") is False


async def test_orphan_reaper_handles_permission_denied_signal():
    ps_output = b"123 /opt/homebrew/bin/container-runtime-linux /containers/worker --uuid worker\n"

    def deny_signal(_pid: int, sig: signal.Signals) -> None:
        if sig == 0:
            raise ProcessLookupError
        raise PermissionError("not owner")

    with (
        patch("pynchy.host.container_manager.process._runtime.is_apple_runtime", True),
        patch(
            "pynchy.host.container_manager.process.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=CompletedProcess(ps_output)),
        ),
        patch("pynchy.host.container_manager.process.os.kill", side_effect=deny_signal),
    ):
        assert await process.reap_apple_runtime_orphans("worker") is True


async def test_orphan_reaper_force_kills_runtime_that_survives_sigterm():
    ps_output = b"123 /opt/homebrew/bin/container-runtime-linux /containers/worker --uuid worker\n"
    alive = True
    signals: list[signal.Signals] = []

    def fake_kill(_pid: int, sig: signal.Signals) -> None:
        nonlocal alive
        if sig == 0:
            if alive:
                return
            raise ProcessLookupError
        signals.append(sig)
        if sig == signal.SIGKILL:
            alive = False

    real_sleep = asyncio.sleep

    async def yield_to_clock(_seconds: float) -> None:
        await real_sleep(0)

    with (
        patch("pynchy.host.container_manager.process._runtime.is_apple_runtime", True),
        patch(
            "pynchy.host.container_manager.process.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=CompletedProcess(ps_output)),
        ),
        patch("pynchy.host.container_manager.process.os.kill", side_effect=fake_kill),
        patch(
            "pynchy.host.container_manager.process._APPLE_RUNTIME_REAP_WAIT_SECONDS",
            0.01,
        ),
        patch(
            "pynchy.host.container_manager.process.asyncio.sleep",
            side_effect=yield_to_clock,
        ),
    ):
        assert await process.reap_apple_runtime_orphans("worker") is True

    assert signals == [signal.SIGTERM, signal.SIGKILL]


async def test_orphan_reaper_tolerates_permission_and_exit_races():
    ps_output = b"123 /opt/homebrew/bin/container-runtime-linux /containers/worker --uuid worker\n"
    pid_probe_calls = 0

    def process_race(_pid: int, sig: signal.Signals) -> None:
        nonlocal pid_probe_calls
        if sig == 0:
            pid_probe_calls += 1
            if pid_probe_calls == 1:
                raise PermissionError("not owner")
            raise ProcessLookupError
        raise ProcessLookupError

    with (
        patch("pynchy.host.container_manager.process._runtime.is_apple_runtime", True),
        patch(
            "pynchy.host.container_manager.process.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=CompletedProcess(ps_output)),
        ),
        patch("pynchy.host.container_manager.process.os.kill", side_effect=process_race),
        patch(
            "pynchy.host.container_manager.process._APPLE_RUNTIME_REAP_WAIT_SECONDS",
            0.0,
        ),
    ):
        assert await process.reap_apple_runtime_orphans("worker") is True

    assert pid_probe_calls == 3


async def test_force_remove_ignores_missing_container_cli():
    with patch(
        "pynchy.host.container_manager.process.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=FileNotFoundError("container missing")),
    ):
        await process.docker_rm_force("worker")
