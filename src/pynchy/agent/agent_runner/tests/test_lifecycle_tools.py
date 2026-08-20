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
    raise AssertionError("publication tool never wrote an IPC request")


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

    result = await asyncio.wait_for(
        call_tool(
            "sync_worktree_to_main",
            {"title": "Fix publication", "body": "## Summary\nFix it."},
        ),
        timeout=2,
    )
    await responder

    assert result.isError is True
    assert result.content[0].text == (
        "owner/pynchy: Push failed: remote rejected write with HTTP 403"
    )
    request_file = next((agent_tool_runtime / "requests").glob("*.json"))
    request = json.loads(request_file.read_text(encoding="utf-8"))
    assert request["payload"]["turn_id"] == "turn-syn-88"
    assert request["payload"]["title"] == "Fix publication"


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

    result = await asyncio.wait_for(
        call_tool(
            "sync_worktree_to_main",
            {"title": "Fix publication", "body": "## Summary\nFix it."},
        ),
        timeout=2,
    )
    await responder

    assert result.isError is True
    assert result.content[0].text == (
        "Publication requires human approval; no branch was published."
    )


@pytest.mark.asyncio
async def test_publish_managed_feature_emits_only_bound_slug_and_returns_result(
    agent_tool_runtime: Path,
) -> None:
    responder = asyncio.create_task(
        _write_publication_result(
            agent_tool_runtime,
            {
                "success": True,
                "message": "Opened PR: https://github.com/owner/repo/pull/42",
            },
        )
    )

    result = await asyncio.wait_for(
        call_tool("publish_managed_feature", {"feature_slug": "safe-feature"}),
        timeout=2,
    )
    await responder

    request_path = next((agent_tool_runtime / "requests").glob("*.json"))
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request["kind"] == "publish_managed_feature"
    assert request["reply_to"] == "merge_results"
    assert request["source_group"] == "pynchy-thread"
    assert request["payload"] == {
        "feature_slug": "safe-feature",
        "publication": "pull-request",
    }
    assert result[0].text.endswith("pull/42")


@pytest.mark.asyncio
async def test_publish_managed_feature_returns_host_error(agent_tool_runtime: Path) -> None:
    responder = asyncio.create_task(
        _write_publication_result(
            agent_tool_runtime,
            {
                "success": False,
                "message": "Publication blocked: managed feature is not active.",
            },
        )
    )

    result = await asyncio.wait_for(
        call_tool("publish_managed_feature", {"feature_slug": "safe-feature"}),
        timeout=2,
    )
    await responder

    assert result.isError is True
    assert result.content[0].text == "Publication blocked: managed feature is not active."


@pytest.mark.asyncio
async def test_rebase_managed_feature_emits_only_bound_slug(agent_tool_runtime: Path) -> None:
    responder = asyncio.create_task(
        _write_publication_result(
            agent_tool_runtime,
            {"success": True, "message": "Rebased managed feature 'safe-feature'."},
        )
    )

    result = await asyncio.wait_for(
        call_tool("rebase_managed_feature", {"feature_slug": "safe-feature"}),
        timeout=2,
    )
    await responder

    request_path = next((agent_tool_runtime / "requests").glob("*.json"))
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request["kind"] == "rebase_managed_feature"
    assert request["payload"] == {"feature_slug": "safe-feature"}
    assert result[0].text == "Rebased managed feature 'safe-feature'."
