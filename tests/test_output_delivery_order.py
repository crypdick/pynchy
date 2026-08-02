"""Public ordering contract for container output delivery."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import patch

from pynchy.host.container_manager.ipc.output_processing import process_output_file

if TYPE_CHECKING:
    from pathlib import Path

    from pynchy.agent_protocol.api import ContainerOutput


async def test_concurrent_output_observers_preserve_emission_order(tmp_path: Path) -> None:
    output_dir = tmp_path / "group" / "output"
    output_dir.mkdir(parents=True)
    first = output_dir / "001.json"
    second = output_dir / "002.json"
    first.write_text(json.dumps({"status": "success", "type": "text", "text": "first"}))
    second.write_text(json.dumps({"status": "success", "type": "text", "text": "second"}))
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    delivered: list[str] = []

    async def handle(output: ContainerOutput) -> None:
        if output.text == "first":
            first_started.set()
            await release_first.wait()
        delivered.append(output.text or "")

    with patch(
        "pynchy.host.container_manager.session.get_session_output_handler",
        return_value=handle,
    ):
        first_task = asyncio.create_task(process_output_file(first, "group", tmp_path))
        await first_started.wait()
        second_task = asyncio.create_task(process_output_file(second, "group", tmp_path))
        await asyncio.sleep(0.01)
        release_first.set()
        await asyncio.gather(first_task, second_task)

    assert delivered == ["first", "second"]
