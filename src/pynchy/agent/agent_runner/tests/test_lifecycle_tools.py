"""Behavioral tests for actionable lifecycle-tool responses."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from agent_runner.agent_tools import AgentToolRuntime, call_tool, use_agent_tool_runtime

if TYPE_CHECKING:
    from pathlib import Path


async def _write_publication_result(ipc_dir: Path, result: dict[str, object]) -> None:
    request_dir = ipc_dir / "requests"
    for _ in range(50):
        request_files = list(request_dir.glob("*.json"))
        if request_files:
            request = json.loads(request_files[0].read_text(encoding="utf-8"))
            result_path = ipc_dir / "merge_results" / f"{request['request_id']}.json"
            result_path.parent.mkdir()
            result_path.write_text(json.dumps(result), encoding="utf-8")
            return
        await asyncio.sleep(0.02)
    raise AssertionError("sync_worktree_to_main never wrote an IPC request")


@pytest.fixture
def agent_tool_runtime(tmp_path: Path):
    with use_agent_tool_runtime(
        AgentToolRuntime(
            chat_jid="discord:thread:syn-88",
            group_folder="pynchy-thread",
            is_admin=False,
            is_scheduled_task=True,
            ipc_dir=tmp_path,
            turn_id="turn-syn-88",
        )
    ):
        yield tmp_path


@pytest.mark.asyncio
async def test_publication_failure_prefers_per_repository_diagnostic(
    agent_tool_runtime: Path,
) -> None:
    responder = asyncio.create_task(
        _write_publication_result(
            agent_tool_runtime,
            {
                "success": False,
                "message": "One or more repo syncs failed.",
                "repos": {
                    "owner/pynchy": {
                        "success": False,
                        "message": "Push failed: remote rejected write with HTTP 403",
                    }
                },
            },
        )
    )

    result = await asyncio.wait_for(call_tool("sync_worktree_to_main", {}), timeout=2)
    await responder

    assert result.isError is True
    assert result.content[0].text == (
        "owner/pynchy: Push failed: remote rejected write with HTTP 403"
    )
    request_file = next((agent_tool_runtime / "requests").glob("*.json"))
    request = json.loads(request_file.read_text(encoding="utf-8"))
    assert request["payload"]["turn_id"] == "turn-syn-88"


@pytest.mark.asyncio
async def test_publication_failure_without_repository_result_keeps_aggregate_diagnostic(
    agent_tool_runtime: Path,
) -> None:
    responder = asyncio.create_task(
        _write_publication_result(
            agent_tool_runtime,
            {
                "success": False,
                "message": "Publication requires human approval; no branch was published.",
            },
        )
    )

    result = await asyncio.wait_for(call_tool("sync_worktree_to_main", {}), timeout=2)
    await responder

    assert result.isError is True
    assert result.content[0].text == (
        "Publication requires human approval; no branch was published."
    )
