"""Behavioral coverage for bounded container-process fallbacks."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from pynchy.host.container_manager import process


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
    container = MagicMock()

    with patch(
        "pynchy.host.container_manager.process._stop_container_process",
        new_callable=AsyncMock,
        side_effect=RuntimeError("stop failed"),
    ):
        await process.graceful_stop(container, "worker")

    container.kill.assert_called_once_with()
