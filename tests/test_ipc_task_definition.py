"""Public IPC seams for reading and editing scheduled task definitions."""

from __future__ import annotations

import json

from conftest import init_test_database, make_settings

from pynchy.host.container_manager.ipc.protocol import request_requires_idempotency_ledger
from pynchy.host.container_manager.ipc.registry import dispatch
from pynchy.host.orchestrator.app import PynchyApp
from pynchy.host.orchestrator.dep_factory import make_ipc_deps
from pynchy.scheduling.api import ScheduledTask, SessionPolicy
from pynchy.state import create_task, get_task_by_id


async def _seed_tasks() -> None:
    for task in (
        ScheduledTask(
            id="own-task",
            group_folder="review",
            chat_jid="review@example.test",
            prompt="repair the old path",
            schedule_type="cron",
            schedule_value="0 * * * *",
            session_policy=SessionPolicy.RESET_BEFORE_RUN,
        ),
        ScheduledTask(
            id="foreign-task",
            group_folder="other",
            chat_jid="other@example.test",
            prompt="foreign private prompt",
            schedule_type="once",
            schedule_value="2026-08-07T01:00:00+00:00",
            session_policy=SessionPolicy.CONTINUE,
            status="paused",
        ),
        ScheduledTask(
            id="managed-task",
            group_folder="review",
            chat_jid="review@example.test",
            prompt="managed source prompt",
            schedule_type="cron",
            schedule_value="0 9 * * *",
            session_policy=SessionPolicy.RESET_BEFORE_RUN,
            config_job_name="daily-review",
        ),
    ):
        await create_task(task)


async def _dispatch(tmp_path, request: dict[str, object], *, group: str, admin: bool) -> dict:
    request_id = str(request["request_id"])
    await dispatch(request, group, admin, make_ipc_deps(PynchyApp()))
    return json.loads(
        (tmp_path / "ipc" / group / "responses" / f"{request_id}.json").read_text(encoding="utf-8")
    )


async def test_owner_reads_editable_definition(monkeypatch, tmp_path) -> None:
    await init_test_database()
    monkeypatch.setattr("pynchy.config.settings._state.settings", make_settings(data_dir=tmp_path))
    await _seed_tasks()

    response = await _dispatch(
        tmp_path,
        {"type": "task_definition", "request_id": "read-own", "task_id": "own-task"},
        group="review",
        admin=False,
    )

    assert response == {
        "result": {
            "id": "own-task",
            "group": "review",
            "prompt": "repair the old path",
            "schedule_type": "cron",
            "schedule_value": "0 * * * *",
            "session_policy": "reset_before_run",
            "status": "active",
            "memory_enabled": True,
        }
    }


async def test_non_admin_cannot_read_or_update_foreign_task(monkeypatch, tmp_path) -> None:
    await init_test_database()
    monkeypatch.setattr("pynchy.config.settings._state.settings", make_settings(data_dir=tmp_path))
    await _seed_tasks()

    read_response = await _dispatch(
        tmp_path,
        {"type": "task_definition", "request_id": "read-foreign", "task_id": "foreign-task"},
        group="review",
        admin=False,
    )
    update_response = await _dispatch(
        tmp_path,
        {
            "type": "update_scheduled_task",
            "request_id": "update-foreign",
            "task_id": "foreign-task",
            "prompt": "leak attempt",
        },
        group="review",
        admin=False,
    )

    assert read_response == {"error": "Scheduled task not found"}
    assert update_response == {"error": "Scheduled task not found"}
    foreign_task = await get_task_by_id("foreign-task")
    assert foreign_task is not None
    assert foreign_task.prompt == "foreign private prompt"


async def test_update_returns_reconciled_task_and_resumes_through_state_api(
    monkeypatch, tmp_path
) -> None:
    await init_test_database()
    monkeypatch.setattr("pynchy.config.settings._state.settings", make_settings(data_dir=tmp_path))
    await _seed_tasks()

    response = await _dispatch(
        tmp_path,
        {
            "type": "update_scheduled_task",
            "request_id": "admin-update",
            "task_id": "foreign-task",
            "prompt": "use /home/agent/automation-memory",
            "status": "active",
        },
        group="admin",
        admin=True,
    )
    task = await get_task_by_id("foreign-task")

    assert response["result"]["prompt"] == "use /home/agent/automation-memory"
    assert response["result"]["status"] == "active"
    assert task is not None
    assert task.schedule_value == "2026-08-07T01:00:00+00:00"
    assert task.occurrence_generation == 1
    assert task.bound_group_folder is None


async def test_update_rejects_unknown_fields_without_persisting(monkeypatch, tmp_path) -> None:
    await init_test_database()
    monkeypatch.setattr("pynchy.config.settings._state.settings", make_settings(data_dir=tmp_path))
    await _seed_tasks()

    response = await _dispatch(
        tmp_path,
        {
            "type": "update_scheduled_task",
            "request_id": "bad-update",
            "task_id": "own-task",
            "prompt": "valid but rejected",
            "schedule_value": "* * * * *",
        },
        group="review",
        admin=False,
    )

    assert response == {"error": "Invalid scheduled task update"}
    own_task = await get_task_by_id("own-task")
    assert own_task is not None
    assert own_task.prompt == "repair the old path"


async def test_update_rejects_automation_managed_task(monkeypatch, tmp_path) -> None:
    await init_test_database()
    monkeypatch.setattr("pynchy.config.settings._state.settings", make_settings(data_dir=tmp_path))
    await _seed_tasks()

    response = await _dispatch(
        tmp_path,
        {
            "type": "update_scheduled_task",
            "request_id": "managed-update",
            "task_id": "managed-task",
            "prompt": "would be overwritten",
        },
        group="review",
        admin=False,
    )

    assert response == {"error": "Scheduled task is managed by its automation definition"}
    managed_task = await get_task_by_id("managed-task")
    assert managed_task is not None
    assert managed_task.prompt == "managed source prompt"


def test_task_definition_read_skips_ledger_but_update_uses_it() -> None:
    assert not request_requires_idempotency_ledger("task_definition")
    assert request_requires_idempotency_ledger("update_scheduled_task")
