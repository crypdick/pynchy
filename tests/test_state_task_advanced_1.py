"""Tests for the database layer."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from sqlite3 import IntegrityError

import pytest
from freezegun import freeze_time

from pynchy.agent_protocol.api import (
    InFlightTurn,
    InFlightWorkKind,
)
from pynchy.host.orchestrator.temporal.schedules import agent_task_workflow_id
from pynchy.scheduling.api import (
    ScheduledTask,
    SessionPolicy,
    TaskRunLog,
)
from pynchy.state import (
    begin_in_flight_turn,
    clear_in_flight_turn,
    create_task,
    create_task_if_absent,
    delete_task,
    get_active_task_for_group,
    get_all_tasks,
    get_in_flight_turn,
    get_in_flight_turn_for_task,
    get_task_by_id,
    get_task_run_logs,
    get_tasks_for_conversation,
    get_tasks_for_group,
    log_task_run,
    record_task_completion,
    record_terminal_scheduled_task_failure,
    resume_once_task_after_unclaimed_scheduled_turn,
    resume_task,
    resume_task_if_no_in_flight_turn,
    update_task,
)

pytest_plugins = ("tests.state_support",)


class TestTaskAdvanced:
    """Tests for task querying and lifecycle functions."""

    _TASK_TEMPLATE = ScheduledTask(
        id="",
        group_folder="main",
        chat_jid="group@g.us",
        prompt="test prompt",
        schedule_type="cron",
        schedule_value="0 * * * *",
        session_policy=SessionPolicy.RESET_BEFORE_RUN,
        status="active",
        created_at="2024-01-01T00:00:00.000Z",
    )

    @pytest.mark.parametrize(
        ("create", "expected_next_run"),
        [(create_task, None), (create_task_if_absent, "2026-07-29T00:00:00+00:00")],
    )
    async def test_creation_preserves_task_definition_and_occurrence(
        self, create, expected_next_run
    ):
        task = replace(
            self._TASK_TEMPLATE,
            id="defined-task",
            next_run="2026-07-29T00:00:00+00:00",
            memory_enabled=False,
            repo_access="owner/pynchy",
            input_source="webhook",
            config_job_name="watchdog",
            config_job_is_deterministic=True,
            config_job_command="scripts/watchdog.py",
            config_job_cwd="/srv/watchdog",
            config_job_timeout_seconds=45,
            config_job_display_name="Watchdog",
            config_job_pre_run_command="scripts/prepare.py",
            config_job_pre_run_cwd="/srv/prepare",
            config_job_pre_run_timeout_seconds=20,
            derived_thread_name="Watchdog runs",
            bound_chat_jid="thread@g.us",
            bound_group_folder="main/thread",
            conversation_id="watchdog-conversation",
            last_reset_occurrence="2026-07-28T00:00:00+00:00",
            occurrence_generation=2,
            occurrence_due_at="2026-07-29T00:00:00+00:00",
            superseded_occurrence_generation=1,
            superseded_occurrence_due_at="2026-07-28T00:00:00+00:00",
        )

        await create(task)

        assert await get_task_by_id(task.id) == replace(task, next_run=expected_next_run)
        assert task.next_run == "2026-07-29T00:00:00+00:00"

    async def test_duplicate_creation_keeps_the_original_task(self):
        task = replace(self._TASK_TEMPLATE, id="first-task")
        assert await create_task_if_absent(task) is True
        replacement = replace(task, prompt="Replacement prompt")

        assert await create_task_if_absent(replacement) is False
        with pytest.raises(IntegrityError):
            await create_task(replacement)

        assert await get_all_tasks() == [task]

    async def test_get_tasks_for_group(self):
        await create_task(replace(self._TASK_TEMPLATE, id="t1", next_run=None))
        await create_task(
            replace(self._TASK_TEMPLATE, id="t2", group_folder="other", next_run=None)
        )
        await create_task(replace(self._TASK_TEMPLATE, id="t3", next_run=None))

        tasks = await get_tasks_for_group("main")
        assert len(tasks) == 2
        assert all(t.group_folder == "main" for t in tasks)

    async def test_get_tasks_for_conversation_returns_only_live_owners(self):
        await create_task(
            replace(self._TASK_TEMPLATE, id="active", conversation_id="conversation-1")
        )
        await create_task(
            replace(
                self._TASK_TEMPLATE,
                id="paused",
                conversation_id="conversation-1",
                status="paused",
            )
        )
        await create_task(
            replace(
                self._TASK_TEMPLATE,
                id="cancelled",
                conversation_id="conversation-1",
                status="cancelled",
            )
        )
        await create_task(
            replace(self._TASK_TEMPLATE, id="other", conversation_id="conversation-2")
        )

        tasks = await get_tasks_for_conversation("conversation-1")

        assert {task.id for task in tasks} == {"active", "paused"}

    async def test_get_all_tasks(self):
        await create_task(replace(self._TASK_TEMPLATE, id="t1", next_run=None))
        await create_task(
            replace(self._TASK_TEMPLATE, id="t2", group_folder="other", next_run=None)
        )

        tasks = await get_all_tasks()
        assert len(tasks) == 2

    async def test_get_active_task_for_group(self):
        await create_task(replace(self._TASK_TEMPLATE, id="active-1", next_run=None))
        await create_task(
            replace(self._TASK_TEMPLATE, id="paused-1", status="paused", next_run=None)
        )

        task = await get_active_task_for_group("main")
        assert task is not None
        assert task.id == "active-1"

    async def test_get_active_task_for_group_returns_none(self):
        task = await get_active_task_for_group("nonexistent")
        assert task is None

    async def test_delete_task_clears_unfinished_checkpoint(self):
        task = replace(self._TASK_TEMPLATE, id="cancelled-task", next_run=None)
        await create_task(task)
        await begin_in_flight_turn(
            InFlightTurn(
                turn_id="cancelled-turn",
                chat_jid=task.chat_jid,
                group_folder=task.group_folder,
                work_kind=InFlightWorkKind.SCHEDULED,
                input_messages=[{"content": task.prompt}],
                input_start_cursor="",
                input_end_cursor="",
                started_at="2024-01-01T00:00:00Z",
                task_id=task.id,
            )
        )

        await delete_task(task.id)

        assert await get_in_flight_turn_for_task(task.id) is None

    async def test_update_task_ignores_disallowed_fields(self):
        await create_task(replace(self._TASK_TEMPLATE, id="t1", next_run=None))

        # Try updating a field that isn't in the allowed set
        await update_task("t1", {"invalid_field": "hacked", "status": "paused"})
        task = await get_task_by_id("t1")
        assert task is not None
        assert task.status == "paused"
        assert not hasattr(task, "invalid_field")

    async def test_update_task_allows_chat_jid(self):
        await create_task(replace(self._TASK_TEMPLATE, id="t1", next_run=None))

        await update_task("t1", {"chat_jid": "new@g.us"})
        task = await get_task_by_id("t1")
        assert task is not None
        assert task.chat_jid == "new@g.us"

    async def test_update_task_noop_for_empty_fields(self):
        await create_task(replace(self._TASK_TEMPLATE, id="t1", next_run=None))
        await update_task("t1", {"invalid_field": "value"})
        task = await get_task_by_id("t1")
        assert task is not None
        assert task.status == "active"  # unchanged

    async def test_record_task_completion_sets_completed_for_once(self):
        await create_task(
            replace(
                self._TASK_TEMPLATE,
                id="once-task",
                schedule_type="once",
                next_run="2024-06-01T00:00:00Z",
            )
        )

        await record_task_completion(
            "once-task", last_result="Completed successfully", completed=True
        )
        task = await get_task_by_id("once-task")
        assert task is not None
        assert task.status == "completed"
        assert task.last_result == "Completed successfully"
        assert task.last_run is not None

    async def test_record_task_completion_preserves_recurring_schedule_state(self):
        await create_task(
            replace(self._TASK_TEMPLATE, id="cron-task", next_run="2024-06-01T00:00:00Z")
        )

        await record_task_completion("cron-task", last_result="Done", completed=False)
        task = await get_task_by_id("cron-task")
        assert task is not None
        assert task.status == "active"
        assert task.next_run is None

    async def test_log_task_run(self):
        await create_task(replace(self._TASK_TEMPLATE, id="logged-task", next_run=None))

        await log_task_run(
            TaskRunLog(
                task_id="logged-task",
                run_at="2024-06-01T00:00:00Z",
                duration_ms=1500,
                status="success",
                result="Done",
                error=None,
            )
        )
        await log_task_run(
            TaskRunLog(
                task_id="logged-task",
                run_at="2024-06-01T01:00:00Z",
                duration_ms=500,
                status="error",
                result=None,
                error="Something went wrong",
            )
        )

        # Verify logs exist by deleting the task (which also deletes logs)
        await delete_task("logged-task")
        assert await get_task_by_id("logged-task") is None

    async def test_log_task_run_persists_occurrence_and_temporal_run_metadata(self):
        await create_task(replace(self._TASK_TEMPLATE, id="attempt-task", next_run=None))

        await log_task_run(
            TaskRunLog(
                task_id="attempt-task",
                run_at="2024-06-01T00:00:00Z",
                duration_ms=500,
                status="error",
                result=None,
                error="ValueError: failed on port 12345",
                temporal_workflow_id="workflow-1",
                temporal_workflow_run_id="workflow-run-1",
                temporal_attempt=2,
                turn_id="turn-1",
                error_signature="ValueError: failed on port #",
                escalation_reason="stagnation",
            )
        )

        logs = await get_task_run_logs("attempt-task", limit=1)

        assert len(logs) == 1
        assert logs[0].temporal_workflow_id == "workflow-1"
        assert logs[0].temporal_workflow_run_id == "workflow-run-1"
        assert logs[0].temporal_attempt == 2
        assert logs[0].turn_id == "turn-1"
        assert logs[0].error_signature == "ValueError: failed on port #"
        assert logs[0].escalation_reason == "stagnation"

    async def test_terminal_failure_is_idempotent_per_temporal_run(self):
        await create_task(replace(self._TASK_TEMPLATE, id="terminal-task", next_run=None))

        first = await record_terminal_scheduled_task_failure(
            task_id="terminal-task",
            temporal_workflow_id="workflow-1",
            temporal_workflow_run_id="workflow-run-1",
            error="WorkerShutdown",
        )
        duplicate = await record_terminal_scheduled_task_failure(
            task_id="terminal-task",
            temporal_workflow_id="workflow-1",
            temporal_workflow_run_id="workflow-run-1",
            error="WorkerShutdown",
        )
        next_run = await record_terminal_scheduled_task_failure(
            task_id="terminal-task",
            temporal_workflow_id="workflow-1",
            temporal_workflow_run_id="workflow-run-2",
            error="WorkerShutdown",
        )

        logs = await get_task_run_logs("terminal-task")

        assert (first, duplicate, next_run) == (True, False, True)
        assert [(log.temporal_workflow_id, log.temporal_workflow_run_id) for log in logs] == [
            ("workflow-1", "workflow-run-2"),
            ("workflow-1", "workflow-run-1"),
        ]
        assert {log.escalation_reason for log in logs} == {"temporal_retry_exhausted"}

    async def test_resume_task_preserves_history_and_resets_failure_window(self):
        await create_task(
            replace(self._TASK_TEMPLATE, id="resume-task", next_run=None, status="paused")
        )
        await log_task_run(
            TaskRunLog(
                task_id="resume-task",
                run_at="2024-06-01T00:00:00Z",
                duration_ms=500,
                status="error",
                error="persistent failure",
            )
        )

        await resume_task("resume-task")

        task = await get_task_by_id("resume-task")
        logs = await get_task_run_logs("resume-task")
        assert task is not None
        assert task.status == "active"
        assert task.schedule_value == self._TASK_TEMPLATE.schedule_value
        assert task.occurrence_generation == 0
        assert [log.status for log in logs] == ["resumed", "error"]
        assert logs[0].temporal_workflow_run_id is None
        assert logs[0].turn_id is None

    async def test_once_task_resume_creates_one_fresh_occurrence_under_race(self):
        original = replace(
            self._TASK_TEMPLATE,
            id="resume-once",
            schedule_type="once",
            schedule_value="2026-07-25T05:16:14+00:00",
            status="paused",
            repo_access="crypdick/pynchy",
            conversation_id="conv-linear-issue",
        )
        previous_workflow_id = agent_task_workflow_id(original)
        await create_task(original)
        # Temporal treats the workflow's PAUSED outcome as a successful completion,
        # while Pynchy records the circuit-breaker reason as task error evidence.
        await log_task_run(
            TaskRunLog(
                task_id=original.id,
                run_at="2026-07-25T05:16:14+00:00",
                duration_ms=1,
                status="error",
                error="Same error repeated",
                temporal_workflow_id=previous_workflow_id,
                temporal_workflow_run_id="successful-paused-run",
                escalation_reason="stagnation",
            )
        )

        resumed_at = "2026-07-26T06:00:00+00:00"
        with freeze_time(resumed_at):
            await asyncio.gather(
                resume_task(original.id),
                resume_task(original.id),
                resume_task(original.id),
            )

        resumed = await get_task_by_id(original.id)
        logs = await get_task_run_logs(original.id)
        assert resumed is not None
        assert resumed.status == "active"
        assert resumed.schedule_value == original.schedule_value
        assert resumed.occurrence_due_at == resumed_at
        assert resumed.occurrence_generation == 1
        assert resumed.superseded_occurrence_due_at == original.schedule_value
        assert resumed.superseded_occurrence_generation == 0
        assert resumed.repo_access == original.repo_access
        assert resumed.conversation_id == original.conversation_id
        resumed_workflow_id = agent_task_workflow_id(resumed)
        assert resumed_workflow_id != previous_workflow_id
        assert resumed_workflow_id.endswith("-resume-1")
        assert [log.status for log in logs] == ["resumed", "error"]

        await resume_task(original.id)

        unchanged = await get_task_by_id(original.id)
        assert unchanged is not None
        assert unchanged.occurrence_generation == 1
        assert len(await get_task_run_logs(original.id)) == 2

        await update_task(original.id, {"status": "paused"})
        with freeze_time(resumed_at):
            await resume_task(original.id)

        resumed_again = await get_task_by_id(original.id)
        assert resumed_again is not None
        assert resumed_again.schedule_value == original.schedule_value
        assert resumed_again.occurrence_due_at == resumed_at
        assert resumed_again.occurrence_generation == 2
        assert resumed_again.superseded_occurrence_due_at == resumed_at
        assert resumed_again.superseded_occurrence_generation == 1
        assert agent_task_workflow_id(resumed_again) != resumed_workflow_id
        assert agent_task_workflow_id(resumed_again).endswith("-resume-2")

    async def test_reconciler_resume_refuses_a_live_scheduled_turn(self):
        task = replace(
            self._TASK_TEMPLATE,
            id="resume-guarded",
            schedule_type="once",
            schedule_value="2026-07-26T06:00:00+00:00",
            status="paused",
        )
        await create_task(task)
        await begin_in_flight_turn(
            InFlightTurn(
                turn_id="resume-guarded-turn",
                chat_jid=task.chat_jid,
                group_folder=task.group_folder,
                work_kind=InFlightWorkKind.SCHEDULED,
                input_messages=[],
                input_start_cursor="",
                input_end_cursor="",
                started_at="2026-07-26T06:00:00+00:00",
                task_id=task.id,
                claimed_at="2026-07-26T06:00:01+00:00",
            )
        )

        assert not await resume_task_if_no_in_flight_turn(task.id)
        assert not await resume_once_task_after_unclaimed_scheduled_turn(task.id)
        paused = await get_task_by_id(task.id)
        assert paused is not None
        assert paused.status == "paused"
        assert await get_in_flight_turn("resume-guarded-turn") is not None

        await clear_in_flight_turn("resume-guarded-turn")

        assert await resume_task_if_no_in_flight_turn(task.id)
        active = await get_task_by_id(task.id)
        assert active is not None
        assert active.status == "active"

    async def test_reconciler_resume_retires_an_unclaimed_terminal_scheduled_turn(self):
        task = replace(
            self._TASK_TEMPLATE,
            id="resume-unclaimed",
            schedule_type="once",
            schedule_value="2026-07-26T06:00:00+00:00",
            status="paused",
        )
        await create_task(task)
        await begin_in_flight_turn(
            InFlightTurn(
                turn_id="resume-unclaimed-turn",
                chat_jid=task.chat_jid,
                group_folder=task.group_folder,
                work_kind=InFlightWorkKind.SCHEDULED,
                input_messages=[],
                input_start_cursor="",
                input_end_cursor="",
                started_at="2026-07-26T06:00:00+00:00",
                task_id=task.id,
            )
        )

        assert await resume_once_task_after_unclaimed_scheduled_turn(task.id)
        active = await get_task_by_id(task.id)
        assert active is not None
        assert active.status == "active"
        assert active.occurrence_generation == 1
        assert await get_in_flight_turn_for_task(task.id) is None

    async def test_reconciler_resume_preserves_mixed_scheduled_checkpoints(self):
        task = replace(
            self._TASK_TEMPLATE,
            id="resume-mixed-checkpoints",
            schedule_type="once",
            schedule_value="2026-07-26T06:00:00+00:00",
            status="paused",
        )
        await create_task(task)
        for turn_id, claimed_at in (
            ("terminal-unclaimed-turn", None),
            ("live-claimed-turn", "2026-07-26T06:00:01+00:00"),
        ):
            await begin_in_flight_turn(
                InFlightTurn(
                    turn_id=turn_id,
                    chat_jid=task.chat_jid,
                    group_folder=task.group_folder,
                    work_kind=InFlightWorkKind.SCHEDULED,
                    input_messages=[],
                    input_start_cursor="",
                    input_end_cursor="",
                    started_at="2026-07-26T06:00:00+00:00",
                    task_id=task.id,
                    claimed_at=claimed_at,
                )
            )

        assert not await resume_once_task_after_unclaimed_scheduled_turn(task.id)
        paused = await get_task_by_id(task.id)
        assert paused is not None
        assert paused.status == "paused"
        assert await get_in_flight_turn("terminal-unclaimed-turn") is not None
        assert await get_in_flight_turn("live-claimed-turn") is not None

    async def test_resume_task_ignores_missing_and_non_paused_rows(self):
        completed = replace(
            self._TASK_TEMPLATE,
            id="completed-once",
            schedule_type="once",
            schedule_value="2026-07-25T05:16:14+00:00",
            status="completed",
        )
        await create_task(completed)

        await resume_task(completed.id)
        await resume_task("missing-task")

        unchanged = await get_task_by_id(completed.id)
        assert unchanged is not None
        assert unchanged.status == "completed"
        assert unchanged.schedule_value == completed.schedule_value
        assert unchanged.occurrence_generation == 0
        assert unchanged.occurrence_due_at is None
        assert unchanged.superseded_occurrence_generation is None
        assert unchanged.superseded_occurrence_due_at is None
        assert await get_task_run_logs(completed.id) == []

    async def test_create_task_with_repo_access(self):
        await create_task(
            replace(
                self._TASK_TEMPLATE,
                id="pa-task",
                next_run=None,
                repo_access="owner/pynchy",
            )
        )
        task = await get_task_by_id("pa-task")
        assert task is not None
        assert task.repo_access == "owner/pynchy"

    async def test_create_task_without_repo_access(self):
        await create_task(replace(self._TASK_TEMPLATE, id="no-pa", next_run=None))
        task = await get_task_by_id("no-pa")
        assert task is not None
        assert task.repo_access is None
