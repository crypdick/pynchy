"""Read-only scheduled-work health projection from host state to agents."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

from conftest import init_test_database, make_settings

from pynchy.host.container_manager.ipc.protocol import request_requires_idempotency_ledger
from pynchy.host.container_manager.ipc.registry import dispatch
from pynchy.host.orchestrator.app import PynchyApp
from pynchy.host.orchestrator.dep_factory import make_ipc_deps
from pynchy.scheduling.api import (
    HostJob,
    ScheduledTask,
    SessionPolicy,
    TaskRunLog,
)
from pynchy.state import create_host_job, create_task, log_task_run, record_task_completion


def _orchestration_states(
    tasks: list[ScheduledTask], jobs: list[HostJob], _address: str, _namespace: str
) -> dict[tuple[str, str], dict[str, Any]]:
    states: dict[tuple[str, str], dict[str, Any]] = {}
    for task in tasks:
        states["task", task.id] = {
            "source": "temporal",
            "state": "scheduled" if task.status == "active" else "inactive",
            "next_run": "2026-07-22T04:00:00+00:00" if task.status == "active" else None,
            "schedule_id": f"schedule-{task.id}" if task.status == "active" else None,
            "workflow_id": None,
            "error": "private Temporal error" if task.id == "own-task" else None,
        }
    for job in jobs:
        states["host_job", job.id] = {
            "source": "temporal",
            "state": "scheduled",
            "next_run": "2026-07-22T05:00:00+00:00",
            "schedule_id": f"schedule-{job.id}",
            "workflow_id": None,
            "error": None,
        }
    return states


async def _seed_scheduled_work() -> None:
    for task in (
        ScheduledTask(
            id="own-task",
            group_folder="review",
            chat_jid="review@example.test",
            prompt="private own prompt",
            schedule_type="cron",
            schedule_value="0 * * * *",
            session_policy=SessionPolicy.RESET_BEFORE_RUN,
            last_result="Blocked: missing provider credential",
        ),
        ScheduledTask(
            id="foreign-task",
            group_folder="other",
            chat_jid="other@example.test",
            prompt="private foreign prompt",
            schedule_type="cron",
            schedule_value="30 * * * *",
            session_policy=SessionPolicy.RESET_BEFORE_RUN,
            status="paused",
        ),
    ):
        await create_task(task)
    await record_task_completion(
        "own-task",
        last_result="Blocked: missing provider credential",
        completed=False,
    )
    await log_task_run(
        TaskRunLog(
            task_id="own-task",
            run_at="2026-07-22T03:00:00+00:00",
            duration_ms=1200,
            status="error",
            error="provider unavailable",
            error_signature="ProviderUnavailable",
            temporal_workflow_id="workflow-own-task",
            temporal_attempt=2,
        )
    )
    await create_host_job(
        {
            "id": "host-maintenance",
            "name": "maintenance",
            "command": "private-command --secret-like-argument",
            "schedule_type": "cron",
            "schedule_value": "0 5 * * *",
            "status": "active",
            "created_at": "2026-07-22T01:00:00+00:00",
            "created_by": "admin",
        }
    )


async def _request_status(tmp_path, *, source_group: str, is_admin: bool) -> str:
    request_id = f"status-{source_group}"
    await dispatch(
        {"type": "task_status", "request_id": request_id},
        source_group,
        is_admin,
        make_ipc_deps(PynchyApp()),
    )
    return (tmp_path / "ipc" / source_group / "responses" / f"{request_id}.json").read_text(
        encoding="utf-8"
    )


async def test_non_admin_sees_only_own_task_health_without_private_definitions(
    monkeypatch, tmp_path
) -> None:
    await init_test_database()
    monkeypatch.setattr("pynchy.config.settings._state.settings", make_settings(data_dir=tmp_path))
    monkeypatch.setattr(
        "pynchy.host.orchestrator.dep_factory.get_temporal_orchestration_states",
        AsyncMock(side_effect=_orchestration_states),
    )
    await _seed_scheduled_work()

    response_text = await _request_status(tmp_path, source_group="review", is_admin=False)
    result = json.loads(response_text)["result"]

    assert [task["id"] for task in result["tasks"]] == ["own-task"]
    assert result["tasks"][0]["last_result"] == "Blocked: missing provider credential"
    assert result["tasks"][0]["run_health"]["consecutive_failures"] == 1
    assert result["tasks"][0]["health_reasons"] == [
        "recent_failure",
        "scheduler_error",
        "failure_shaped_result",
    ]
    assert result["tasks"][0]["next_run"] == "2026-07-22T04:00:00+00:00"
    assert result["host_jobs"] == []
    assert "private own prompt" not in response_text
    assert "private foreign prompt" not in response_text
    assert "private-command" not in response_text


async def test_admin_sees_all_task_and_host_job_status(monkeypatch, tmp_path) -> None:
    await init_test_database()
    monkeypatch.setattr("pynchy.config.settings._state.settings", make_settings(data_dir=tmp_path))
    monkeypatch.setattr(
        "pynchy.host.orchestrator.dep_factory.get_temporal_orchestration_states",
        AsyncMock(side_effect=_orchestration_states),
    )
    await _seed_scheduled_work()

    response_text = await _request_status(tmp_path, source_group="admin", is_admin=True)
    result = json.loads(response_text)["result"]

    assert {task["id"] for task in result["tasks"]} == {"own-task", "foreign-task"}
    assert result["host_jobs"] == [
        {
            "id": "host-maintenance",
            "name": "maintenance",
            "schedule_type": "cron",
            "schedule_value": "0 5 * * *",
            "status": "active",
            "enabled": True,
            "next_run": "2026-07-22T05:00:00+00:00",
            "orchestration": {
                "source": "temporal",
                "state": "scheduled",
                "next_run": "2026-07-22T05:00:00+00:00",
                "schedule_id": "schedule-host-maintenance",
                "workflow_id": None,
                "error": None,
            },
            "last_run": None,
            "health_reasons": [],
        }
    ]
    assert result["coverage"]["task_prompts_included"] is False
    assert result["coverage"]["host_commands_included"] is False
    assert "private own prompt" not in response_text
    assert "private-command" not in response_text


async def test_admin_status_includes_rows_beyond_former_agent_caps(monkeypatch, tmp_path) -> None:
    await init_test_database()
    monkeypatch.setattr("pynchy.config.settings._state.settings", make_settings(data_dir=tmp_path))
    monkeypatch.setattr(
        "pynchy.host.orchestrator.dep_factory.get_temporal_orchestration_states",
        AsyncMock(side_effect=_orchestration_states),
    )
    task_ids = [f"task-{index}" for index in range(65)]
    host_job_ids = [f"host-{index}" for index in range(33)]

    for task_id in task_ids:
        await create_task(
            ScheduledTask(
                id=task_id,
                group_folder="bulk",
                chat_jid="bulk@example.test",
                prompt="Check scheduled work.",
                schedule_type="cron",
                schedule_value="0 * * * *",
                session_policy=SessionPolicy.RESET_BEFORE_RUN,
            )
        )
    for index, host_job_id in enumerate(host_job_ids):
        await create_host_job(
            {
                "id": host_job_id,
                "name": f"host job {index}",
                "command": "scripts/check.sh",
                "schedule_type": "cron",
                "schedule_value": "0 * * * *",
                "status": "active",
                "created_at": f"2026-07-22T01:{index:02d}:00+00:00",
                "created_by": "admin",
            }
        )

    response_text = await _request_status(tmp_path, source_group="admin", is_admin=True)
    result = json.loads(response_text)["result"]

    assert {task["id"] for task in result["tasks"]} == set(task_ids)
    assert {job["id"] for job in result["host_jobs"]} == set(host_job_ids)


async def test_task_status_without_request_id_produces_no_response(monkeypatch, tmp_path) -> None:
    await init_test_database()
    monkeypatch.setattr("pynchy.config.settings._state.settings", make_settings(data_dir=tmp_path))

    await dispatch(
        {"type": "task_status"},
        "review",
        False,
        make_ipc_deps(PynchyApp()),
    )

    assert not (tmp_path / "ipc" / "review" / "responses").exists()


def test_task_status_requests_skip_mutation_ledger() -> None:
    assert not request_requires_idempotency_ledger("task_status")
