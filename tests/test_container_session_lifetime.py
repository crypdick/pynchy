"""Container lifetime stays owned through runtime probes and cleanup failures."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from pynchy.host.container_manager.session import (
    ContainerSession,
    RuntimeMonitorPolicy,
    destroy_session,
    get_session,
)
from tests.container_runner_support import FakeProcess, create_session


async def test_cancelled_idle_expiry_does_not_notify_or_stop_a_new_query(tmp_path):
    queued = asyncio.Event()
    resume = asyncio.Event()
    finished = asyncio.Event()

    async def delayed(coro):
        queued.set()
        await resume.wait()
        try:
            await coro
        finally:
            finished.set()

    with (
        patch("pynchy.host.container_manager.session.graceful_stop", new=AsyncMock()),
        patch("pynchy.host.container_manager.session.docker_rm_force", new=AsyncMock()),
    ):
        session = await create_session(
            "queued-idle", "pynchy-queued", FakeProcess(), data_dir=tmp_path, idle_timeout=0.01
        )
        on_idle = AsyncMock()
        session.set_idle_callback(on_idle)
        try:
            # Hold the scheduled teardown until a subsequent query takes ownership.
            with patch(
                "pynchy.host.container_manager.session.create_background_task",
                side_effect=lambda coro, **kw: asyncio.create_task(delayed(coro), **kw),
            ):
                await queued.wait()
                session.set_output_handler(AsyncMock(), query_id="next-query")
                resume.set()
                await finished.wait()
            on_idle.assert_not_awaited()
            assert get_session("queued-idle") is session
        finally:
            await destroy_session("queued-idle")


@pytest.mark.parametrize("replace", [False, True])
async def test_expired_session_cannot_destroy_a_subsequent_query(tmp_path, replace):
    callback_started = asyncio.Event()
    finish_callback = asyncio.Event()
    callback_finished = asyncio.Event()

    async def on_idle():
        callback_started.set()
        await finish_callback.wait()
        callback_finished.set()

    with (
        patch("pynchy.host.container_manager.session.graceful_stop", new=AsyncMock()),
        patch("pynchy.host.container_manager.session.docker_rm_force", new=AsyncMock()),
    ):
        old = await create_session(
            "replace-idle", "pynchy-old", FakeProcess(), data_dir=tmp_path, idle_timeout=0.01
        )
        old.set_idle_callback(on_idle)
        await callback_started.wait()
        replacement = (
            await create_session(
                "replace-idle", "pynchy-new", FakeProcess(), data_dir=tmp_path, idle_timeout=0.0
            )
            if replace
            else old
        )
        replacement.set_output_handler(AsyncMock(), query_id="next-query")
        try:
            finish_callback.set()
            await callback_finished.wait()
            await asyncio.sleep(0)
            assert get_session("replace-idle") is replacement
            assert replacement.is_alive
        finally:
            await destroy_session("replace-idle")


async def test_cli_exit_keeps_session_alive_until_runtime_probe_completes():
    probing = asyncio.Event()
    release = asyncio.Event()

    async def runtime_probe(_name: str) -> bool:
        probing.set()
        await release.wait()
        return True

    session = ContainerSession("probe", "pynchy-probe", runtime_probe=runtime_probe)
    proc = FakeProcess()
    session.start(proc)
    proc.close(code=1)
    await probing.wait()
    try:
        assert session.is_alive is True
    finally:
        with patch("pynchy.host.container_manager.session.docker_rm_force", new=AsyncMock()):
            await session.stop()


async def test_cli_exit_invalidates_in_flight_startup_probe():
    probing = asyncio.Event()
    release = asyncio.Event()
    confirmed_running = asyncio.Event()
    calls = 0

    async def runtime_probe(_name: str) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            probing.set()
            await release.wait()
            return False
        confirmed_running.set()
        return True

    session = ContainerSession(
        "startup-probe",
        "pynchy-startup-probe",
        runtime_probe=runtime_probe,
        runtime_monitor_policy=RuntimeMonitorPolicy(start_grace_seconds=0.0),
    )
    proc = FakeProcess()
    with (
        patch("pynchy.host.container_manager.session.sys.platform", "darwin"),
        patch("pynchy.host.container_manager.session.reap_apple_runtime_orphans", new=AsyncMock()),
        patch("pynchy.host.container_manager.session.docker_rm_force", new=AsyncMock()),
    ):
        session.start(proc)
        await probing.wait()
        proc.close(code=1)
        release.set()
        await confirmed_running.wait()
        await asyncio.sleep(0)
        try:
            assert proc._killed is False
            assert session.is_alive is True
        finally:
            await session.stop()


@pytest.mark.parametrize("failure", ["graceful_stop", "docker_rm_force", "clean_secret_files"])
async def test_stop_finishes_owned_cleanup_even_when_one_step_fails(failure):
    probing = asyncio.Event()
    stopped = asyncio.Event()

    async def runtime_probe(_name: str) -> bool:
        probing.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return True

    operations = {
        "graceful_stop": AsyncMock(),
        "docker_rm_force": AsyncMock(),
        "clean_secret_files": Mock(),
    }
    error = OSError("cleanup failed")
    operations[failure].side_effect = error
    session = ContainerSession("cleanup", "pynchy-cleanup", runtime_probe=runtime_probe)
    proc = FakeProcess()
    with (
        patch("pynchy.host.container_manager.session.sys.platform", "darwin"),
        patch.multiple("pynchy.host.container_manager.session", **operations),
    ):
        session.start(proc)
        await probing.wait()
        session.set_output_handler(AsyncMock(), query_id="query")
        try:
            with pytest.raises(OSError, match="cleanup failed") as raised:
                await session.stop()
            assert raised.value is error
            assert stopped.is_set()
            operations["docker_rm_force"].assert_awaited_once()
            operations["clean_secret_files"].assert_called_once()
            await session.wait_for_query_done(query_timeout_seconds=0.1)
            assert session.is_alive is False
        finally:
            operations[failure].side_effect = None
            await session.stop()
