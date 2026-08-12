"""Public list_tasks behavior for malformed host status projections."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from mcp.types import TextContent

sys.path.insert(
    0, str(Path(__file__).parent.parent / "src" / "pynchy" / "agent" / "agent_runner" / "src")
)

from agent_runner.agent_tools import AgentToolRuntime, call_tool, use_agent_tool_runtime


def _runtime(tmp_path: Path) -> AgentToolRuntime:
    return AgentToolRuntime(
        chat_jid="test@g.us",
        group_folder="test-group",
        is_admin=True,
        is_scheduled_task=False,
        ipc_dir=tmp_path,
    )


@pytest.mark.asyncio
@pytest.mark.action("task.list")
@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        ("invalid_json", "host task status is not valid JSON"),
        ("non_object", "host task status must be an object"),
        ("bad_tasks", "tasks must be an array of objects"),
        ("bad_host_jobs", "host_jobs must be an array of objects"),
        ("missing_id", "id must be a non-empty string"),
        ("bad_next_run", "next_run must be a string or null"),
        ("bad_evidence", "status evidence must be a string or null"),
        ("bad_failures", "consecutive_failures must be an integer from 0 to 9999"),
        ("bad_enabled", "enabled must be a boolean"),
        ("long_id", "id exceeds the 128-character contract"),
        ("long_next_run", "next_run exceeds the 64-character contract"),
        ("missing_orchestration", "orchestration must be an object"),
    ],
)
async def test_list_tasks_rejects_malformed_host_projection(
    monkeypatch, tmp_path: Path, variant: str, expected: str
) -> None:
    task = {
        "id": "task-1",
        "group": "admin",
        "schedule_type": "cron",
        "schedule_value": "0 9 * * *",
        "status": "active",
        "next_run": None,
        "orchestration": {"state": "scheduled", "error": None},
        "run_health": {"last_status": "success", "consecutive_failures": 0},
        "health_reasons": ["missing_next_run"],
    }
    host_job = {
        "id": "job-1",
        "name": "backup",
        "schedule_type": "cron",
        "schedule_value": "0 1 * * *",
        "status": "active",
        "enabled": True,
        "next_run": None,
        "orchestration": {"state": "scheduled", "error": None},
        "health_reasons": ["missing_next_run"],
    }
    status: object = {"tasks": [task], "host_jobs": [host_job]}
    raw_status = ""
    if variant == "invalid_json":
        raw_status = "not json"
    elif variant == "non_object":
        status = []
    elif variant == "bad_tasks":
        status = {"tasks": ["not an object"], "host_jobs": []}
    elif variant == "bad_host_jobs":
        status = {"tasks": [], "host_jobs": ["not an object"]}
    elif variant == "missing_id":
        task.pop("id")
    elif variant == "bad_next_run":
        task["next_run"] = 42
    elif variant == "bad_evidence":
        task["last_result"] = {"value": "x"}
    elif variant == "bad_failures":
        task["run_health"] = {"last_status": "error", "consecutive_failures": True}
    elif variant == "bad_enabled":
        host_job["enabled"] = "yes"
    elif variant == "long_id":
        task["id"] = "x" * 129
    elif variant == "long_next_run":
        task["next_run"] = "x" * 65
    elif variant == "missing_orchestration":
        task.pop("orchestration")

    monkeypatch.setattr(
        "agent_runner.agent_tools._tools_tasks.ipc_service_request",
        AsyncMock(return_value=[TextContent(type="text", text=raw_status or json.dumps(status))]),
    )

    with use_agent_tool_runtime(_runtime(tmp_path)):
        result = await call_tool("list_tasks", {})

    assert result.isError is True
    assert expected in result.content[0].text


@pytest.mark.asyncio
@pytest.mark.action("task.list")
async def test_list_tasks_omits_blank_evidence_and_marks_host_attention(
    monkeypatch, tmp_path: Path
) -> None:
    status = {
        "tasks": [
            {
                "id": "task-1",
                "group": "admin",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "status": "active",
                "next_run": "2026-07-23T16:00:00+00:00",
                "last_result": "   ",
                "orchestration": {"state": "scheduled", "error": None},
                "run_health": {"last_status": "success", "consecutive_failures": 0},
                "health_reasons": [],
            }
        ],
        "host_jobs": [
            {
                "id": "job-1",
                "name": "backup",
                "schedule_type": "cron",
                "schedule_value": "0 1 * * *",
                "status": "paused",
                "enabled": True,
                "next_run": None,
                "orchestration": {"state": "scheduled", "error": None},
                "health_reasons": ["paused"],
            }
        ],
    }
    monkeypatch.setattr(
        "agent_runner.agent_tools._tools_tasks.ipc_service_request",
        AsyncMock(return_value=[TextContent(type="text", text=json.dumps(status))]),
    )

    with use_agent_tool_runtime(_runtime(tmp_path)):
        result = await call_tool("list_tasks", {})

    payload = result[1]
    assert "last_result" not in payload["tasks"][0]
    assert payload["host_jobs"][0]["health_reasons"] == ["paused"]
