"""Behavioral coverage for persistent session cleanup boundaries."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pynchy.host.container_manager import session as session_mod
from tests.container_runner_support import FakeProcess, create_session


async def test_idle_expiry_destroys_session_without_an_idle_callback():
    session = session_mod.ContainerSession("idle-group", "pynchy-idle-group")
    destroy = AsyncMock()

    session.set_idle_timeout(0.01)
    with patch("pynchy.host.container_manager.session.destroy_session", destroy):
        session.signal_query_done()
        await asyncio.sleep(0.05)

    destroy.assert_awaited_once_with("idle-group")


async def test_stop_unblocks_runtime_monitor_that_finishes_after_shutdown_starts():
    runtime_running = True
    runtime_probe_started = asyncio.Event()
    release_runtime_probe = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    probe_calls = 0

    async def runtime_probe(_container_name: str) -> bool:
        nonlocal probe_calls
        probe_calls += 1
        if probe_calls == 1:
            return True
        runtime_probe_started.set()
        await release_runtime_probe.wait()
        return runtime_running

    async def wait_for_cleanup(_container_name: str) -> None:
        cleanup_started.set()
        await release_cleanup.wait()

    session = session_mod.ContainerSession(
        "stopping-group",
        "pynchy-stopping-group",
        runtime_probe=runtime_probe,
    )
    proc = FakeProcess()
    session.start(proc)
    proc.close(code=1)
    await asyncio.wait_for(runtime_probe_started.wait(), timeout=0.2)

    with patch(
        "pynchy.host.container_manager.session.docker_rm_force",
        side_effect=wait_for_cleanup,
    ):
        stop_task = asyncio.create_task(session.stop())
        await asyncio.wait_for(cleanup_started.wait(), timeout=0.2)

        runtime_running = False
        release_runtime_probe.set()
        await asyncio.sleep(0)
        release_cleanup.set()
        await stop_task

    assert session.is_alive is False


async def test_stop_marks_session_dead_before_process_monitor_observes_exit():
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def wait_for_cleanup(_container_name: str) -> None:
        cleanup_started.set()
        await release_cleanup.wait()

    session = session_mod.ContainerSession("stop-group", "pynchy-stop-group")
    proc = FakeProcess()
    session.start(proc)

    with (
        patch("pynchy.host.container_manager.session.graceful_stop", new=AsyncMock()),
        patch(
            "pynchy.host.container_manager.session.docker_rm_force",
            side_effect=wait_for_cleanup,
        ),
    ):
        stop_task = asyncio.create_task(session.stop())
        await asyncio.wait_for(cleanup_started.wait(), timeout=0.2)
        proc.close(code=1)
        await asyncio.sleep(0)
        release_cleanup.set()
        await stop_task

    assert session.is_alive is False


async def test_stderr_reader_logs_nonempty_lines_and_ignores_blank_lines():
    session = session_mod.ContainerSession("stderr-group", "pynchy-stderr-group")
    proc = FakeProcess()

    with patch("pynchy.host.container_manager.session.logger.debug") as debug:
        session.start(proc)
        proc.emit_stderr(b" first line\n\nsecond line\n")
        proc.stderr.feed_eof()
        await asyncio.sleep(0)

    assert [call.args[0] for call in debug.call_args_list] == [
        "first line",
        "second line",
    ]

    proc.close()
    with (
        patch("pynchy.host.container_manager.session.graceful_stop", new=AsyncMock()),
        patch("pynchy.host.container_manager.session.docker_rm_force", new=AsyncMock()),
    ):
        await session.stop()


async def test_create_session_stops_existing_session_before_replacing_it():
    first_proc = FakeProcess()
    second_proc = FakeProcess()

    with (
        patch("pynchy.host.container_manager.session.docker_rm_force", new=AsyncMock()),
        patch("pynchy.host.container_manager.session.graceful_stop", new=AsyncMock()),
    ):
        first = await create_session(
            "replace-group",
            "pynchy-first",
            first_proc,
            data_dir=Path("unused-data"),
            idle_timeout=0.0,
        )
        first_proc.close()
        await asyncio.sleep(0)

        with patch.object(first, "stop", new=AsyncMock()) as stop_old:
            second = await create_session(
                "replace-group",
                "pynchy-second",
                second_proc,
                data_dir=Path("unused-data"),
                idle_timeout=0.0,
            )

        stop_old.assert_awaited_once_with()
        assert session_mod.get_session("replace-group") is second

        second_proc.close()
        await asyncio.sleep(0)
        await session_mod.destroy_session("replace-group")


async def test_create_session_keeps_startup_alive_when_stale_output_cannot_be_deleted(
    tmp_path: Path,
):
    output_dir = tmp_path / "ipc" / "stale-group" / "output"
    output_dir.mkdir(parents=True)
    stale_output = output_dir / "old.json"
    stale_output.write_text("stale")

    with (
        patch("pathlib.Path.unlink", side_effect=OSError("output busy")),
        patch("pynchy.host.container_manager.session.graceful_stop", new=AsyncMock()),
        patch("pynchy.host.container_manager.session.docker_rm_force", new=AsyncMock()),
    ):
        session = await create_session(
            "stale-group",
            "pynchy-stale-group",
            FakeProcess(),
            data_dir=tmp_path,
            idle_timeout=0.0,
        )
        assert session_mod.get_session("stale-group") is session
        await session_mod.destroy_session("stale-group")

    assert stale_output.exists()
