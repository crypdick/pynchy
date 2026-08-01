"""Public agent-runner lifecycle behavior at shutdown and error boundaries."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(
    0,
    str(Path(__file__).parent.parent / "src" / "pynchy" / "agent" / "agent_runner" / "src"),
)

from agent_runner.events import TextEvent
from agent_runner.main import main as run_agent_main
from agent_runner.models import ContainerInput, ContainerOutput


def _input() -> ContainerInput:
    return ContainerInput(
        messages=[],
        group_folder="test-group",
        chat_jid="test@g.us",
        is_admin=False,
    )


@pytest.mark.asyncio
async def test_close_during_query_stops_without_waiting_for_followup(tmp_path: Path):
    class CloseCore:
        session_id = ""

        async def start(self) -> None:
            return None

        async def query(self, _prompt: str):
            yield TextEvent(text="partial")

        async def stop(self) -> None:
            self.stopped = True

    core = CloseCore()
    outputs: list[ContainerOutput] = []
    with (
        patch("agent_runner.main.read_initial_input", return_value=_input()),
        patch("agent_runner.main.build_initial_prompt", return_value="prompt"),
        patch("agent_runner.main.create_agent_core", return_value=core),
        patch("agent_runner.main.should_close", return_value=True),
        patch("agent_runner.main.wait_for_ipc_followup", new_callable=AsyncMock) as wait,
        patch("agent_runner.main.write_output", side_effect=outputs.append),
        patch("agent_runner.main.IPC_INPUT_DIR", tmp_path / "input"),
        patch("agent_runner.main.IPC_INPUT_CLOSE_SENTINEL", tmp_path / "_close"),
    ):
        await run_agent_main()

    assert outputs == []
    wait.assert_not_awaited()
    assert core.stopped is True


@pytest.mark.asyncio
async def test_query_and_cleanup_failures_are_reported_without_masking_query_error(
    tmp_path: Path,
):
    class FailingCore:
        session_id = ""

        async def start(self) -> None:
            return None

        async def query(self, prompt: str):
            if prompt == "__never__":
                yield TextEvent(text="unreachable")
            raise RuntimeError("query failed")

        async def stop(self) -> None:
            raise RuntimeError("cleanup failed")

    outputs: list[ContainerOutput] = []
    with (
        patch("agent_runner.main.read_initial_input", return_value=_input()),
        patch("agent_runner.main.build_initial_prompt", return_value="prompt"),
        patch("agent_runner.main.create_agent_core", return_value=FailingCore()),
        patch("agent_runner.main.write_output", side_effect=outputs.append),
        patch("agent_runner.main.IPC_INPUT_DIR", tmp_path / "input"),
        patch("agent_runner.main.IPC_INPUT_CLOSE_SENTINEL", tmp_path / "_close"),
        pytest.raises(SystemExit),
    ):
        await run_agent_main()

    assert len(outputs) == 1
    assert outputs[0].status == "error"
    assert outputs[0].error == "query failed"
