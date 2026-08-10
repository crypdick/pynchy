"""Tests for task scheduler.

Tests the scheduled task execution logic, including:
- Scheduler loop initialization and duplicate prevention
- Temporal reconciliation handoff
- Task execution with different context modes
- Next run calculation for cron, interval, and once schedules
- Error handling and logging
- Group lookup and validation
"""

from __future__ import annotations

# ruff: noqa: SIM117
import asyncio
import contextlib
from contextvars import ContextVar
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.agent_protocol.api import (
    CheckpointControlState,
    ContainerOutput,
    InFlightTurn,
    InFlightWorkKind,
)
from pynchy.host.orchestrator import task_scheduler as ts_mod
from pynchy.host.orchestrator.task_scheduler import run_scheduled_agent
from pynchy.scheduling.api import (
    SessionPolicy,
    TaskRunLog,
)
from pynchy.state import (
    begin_in_flight_turn,
    create_task,
    get_in_flight_turn_for_task,
    get_task_by_id,
    get_task_run_logs,
    init_test_database,
    prepare_in_flight_turn_recovery,
)
from pynchy.turn_outcomes import TurnOutcome
from tests.task_scheduler_support import (
    _ActivityInfo,
    _patch_settings,
    _run_due_task_via_scheduler,
)

pytest_plugins = ("tests.task_scheduler_support",)

TEMPORAL_UNAVAILABLE_MESSAGE = "temporal unavailable"
TEST_ERROR_MESSAGE = "Test error"
AGENT_FAILED_MESSAGE = "Agent failed"

_scheduler_settings: ContextVar[object | None] = ContextVar("scheduler_settings", default=None)


class TestRunScheduledAgent:
    """Test task execution logic.

    Since run_scheduled_agent delegates to deps.run_agent (the unified
    entry point), these tests verify that the scheduler correctly constructs
    messages, passes the right flags, handles return values, and logs runs.
    """

    @pytest.fixture(autouse=True)
    async def _isolated_turn_ledger(self):
        await init_test_database()

    @pytest.mark.asyncio
    async def test_pauses_task_before_execution_after_repeated_same_failure(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Repeated identical failures trip the scheduled-task circuit breaker."""
        mock_deps.groups["test-jid"] = sample_group
        previous = [
            TaskRunLog(
                task_id=sample_task.id,
                run_at=f"2024-06-01T00:0{i}:00Z",
                duration_ms=10,
                status="error",
                result=None,
                error="API Error: 429 rate limit on request 123",
                error_signature="API Error: # rate limit on request #",
            )
            for i in range(3)
        ]
        updates = []
        logged_runs = []

        def mock_update(task_id, update):
            updates.append((task_id, update))

        def mock_log_run(log: TaskRunLog):
            logged_runs.append(log)

        with patch(
            "pynchy.host.orchestrator.task_scheduler.get_task_run_logs",
            new_callable=AsyncMock,
            return_value=previous,
        ):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.update_task", side_effect=mock_update
            ):
                with patch(
                    "pynchy.host.orchestrator.task_scheduler.log_task_run",
                    side_effect=mock_log_run,
                ):
                    with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
                        await _run_due_task_via_scheduler(mock_deps, sample_task)

        assert mock_deps.agent_runs == []
        assert updates == [(sample_task.id, {"status": "paused"})]
        assert len(logged_runs) == 1
        assert logged_runs[0].status == "error"
        assert logged_runs[0].escalation_reason == "stagnation"
        assert "Same error repeated" in (logged_runs[0].error or "")

    @pytest.mark.asyncio
    async def test_logs_error_when_group_not_found(self, mock_deps, sample_task, tmp_path):
        """Should log error when group is not registered."""
        logged_runs = []

        def mock_log_run(log: TaskRunLog):
            logged_runs.append(log)

        with patch(
            "pynchy.host.orchestrator.task_scheduler.log_task_run", side_effect=mock_log_run
        ):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock
            ):
                with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
                    await _run_due_task_via_scheduler(mock_deps, sample_task)

        # Should have logged an error
        assert len(logged_runs) == 1
        assert logged_runs[0].status == "error"
        assert "runtime is not registered" in logged_runs[0].error

    @pytest.mark.asyncio
    async def test_calls_run_agent_with_correct_flags(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Should call run_agent with is_scheduled_task=True and input_source='scheduled_task'."""
        mock_deps.groups["test-jid"] = sample_group

        with patch.object(
            mock_deps,
            "automation_memory_dir",
            return_value=contextlib.nullcontext(tmp_path / sample_task.id),
        ):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.log_task_run",
                new_callable=AsyncMock,
            ):
                with patch(
                    "pynchy.host.orchestrator.task_scheduler.record_task_completion",
                    new_callable=AsyncMock,
                ):
                    with patch(
                        "pynchy.host.orchestrator.task_scheduler.update_task",
                        new_callable=AsyncMock,
                    ):
                        with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
                            await _run_due_task_via_scheduler(mock_deps, sample_task)

        assert len(mock_deps.agent_runs) == 1
        run = mock_deps.agent_runs[0]
        assert run["is_scheduled_task"] is True
        assert run["input_source"] == "scheduled_task"
        assert isinstance(run["turn_id"], str)
        assert run["turn_id"].startswith("turn_")
        assert run["chat_jid"] == "test@g.us"
        assert run["automation_memory_dir"].name == sample_task.id
        # Verify prompt was passed as a user message
        assert len(run["messages"]) == 1
        assert run["messages"][0]["content"] == "Test task"
        assert run["messages"][0]["sender"] == "scheduled_task"

    @pytest.mark.asyncio
    async def test_memory_opt_out_skips_directory(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        task = replace(sample_task, memory_enabled=False)
        mock_deps.groups[sample_group.jid] = sample_group

        with (
            patch.object(
                mock_deps,
                "automation_memory_dir",
                side_effect=AssertionError("memory directory must stay disabled"),
            ),
            patch(
                "pynchy.host.orchestrator.task_scheduler.log_task_run",
                new_callable=AsyncMock,
            ),
            patch(
                "pynchy.host.orchestrator.task_scheduler.record_task_completion",
                new_callable=AsyncMock,
            ),
            patch(
                "pynchy.host.orchestrator.task_scheduler.update_task",
                new_callable=AsyncMock,
            ),
            _patch_settings(groups_dir=tmp_path, poll_interval=0.01),
        ):
            await _run_due_task_via_scheduler(mock_deps, task)

        assert mock_deps.agent_runs[0]["automation_memory_dir"] is None
        assert mock_deps.agent_runs[0]["extra_system_notices"] is None

    @pytest.mark.asyncio
    async def test_reset_policy_resets_once_per_occurrence_including_first_run(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Temporal activity retries reuse the reset session created for their occurrence."""
        task = replace(
            sample_task,
            session_policy=SessionPolicy.RESET_BEFORE_RUN,
        )
        mock_deps.groups[sample_group.jid] = sample_group
        mock_deps._run_agent_result = "error"
        await create_task(task)

        with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
            assert (
                await run_scheduled_agent(
                    task,
                    mock_deps,
                    occurrence_id="occurrence-1",
                )
                is TurnOutcome.RETRY
            )
            persisted = await get_task_by_id(task.id)
            assert persisted is not None
            mock_deps._run_agent_result = "success"
            assert (
                await run_scheduled_agent(
                    persisted,
                    mock_deps,
                    occurrence_id="occurrence-1",
                )
                is TurnOutcome.COMPLETED
            )
            persisted = await get_task_by_id(task.id)
            assert persisted is not None
            assert (
                await run_scheduled_agent(
                    persisted,
                    mock_deps,
                    occurrence_id="occurrence-2",
                )
                is TurnOutcome.COMPLETED
            )

        assert mock_deps.context_resets == [
            (task.id, sample_group.jid, "occurrence-1"),
            (task.id, sample_group.jid, "occurrence-2"),
        ]
        persisted = await get_task_by_id(task.id)
        assert persisted is not None
        assert persisted.last_reset_occurrence == "occurrence-2"

    @pytest.mark.asyncio
    async def test_continue_policy_never_resets_between_occurrences(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        mock_deps.groups[sample_group.jid] = sample_group
        await create_task(sample_task)

        with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
            assert (
                await run_scheduled_agent(
                    sample_task,
                    mock_deps,
                    occurrence_id="occurrence-1",
                )
                is TurnOutcome.COMPLETED
            )
            assert (
                await run_scheduled_agent(
                    sample_task,
                    mock_deps,
                    occurrence_id="occurrence-2",
                )
                is TurnOutcome.COMPLETED
            )

        assert mock_deps.context_resets == []

    @pytest.mark.asyncio
    async def test_restart_resumes_interrupted_scheduled_job(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """A killed periodic job continues with its original durable turn ID."""
        mock_deps.groups["test-jid"] = sample_group

        async def interrupted_run(_group, _jid, _messages, on_output, **_kwargs):
            await on_output(ContainerOutput(status="success", result="partial task output"))
            raise asyncio.CancelledError

        mock_deps._run_agent_side_effect = interrupted_run

        with (
            patch("pynchy.host.orchestrator.task_scheduler.log_task_run", new_callable=AsyncMock),
            patch(
                "pynchy.host.orchestrator.task_scheduler.record_task_completion",
                new_callable=AsyncMock,
            ),
            patch("pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock),
            _patch_settings(groups_dir=tmp_path, poll_interval=0.01),
        ):
            with pytest.raises(asyncio.CancelledError):
                await _run_due_task_via_scheduler(mock_deps, sample_task)

            checkpoint = await get_in_flight_turn_for_task(sample_task.id)
            assert checkpoint is not None
            assert checkpoint.output_sent is True
            original_turn_id = checkpoint.turn_id
            await prepare_in_flight_turn_recovery("deploy-sha")

            mock_deps._run_agent_side_effect = None
            assert await _run_due_task_via_scheduler(mock_deps, sample_task) is None

        assert len(mock_deps.agent_runs) == 2
        resumed_run = mock_deps.agent_runs[-1]
        assert resumed_run["turn_id"] == original_turn_id
        assert resumed_run["input_source"] == "scheduled_task"
        assert sample_task.prompt in resumed_run["messages"][0]["content"]
        assert await get_in_flight_turn_for_task(sample_task.id) is None

    @pytest.mark.asyncio
    async def test_later_schedule_trigger_skips_frozen_occurrence(
        self, mock_deps, sample_task, tmp_path
    ):
        sample_task.status = "active"
        await begin_in_flight_turn(
            InFlightTurn(
                turn_id="scheduled-paused",
                chat_jid=sample_task.chat_jid,
                group_folder=sample_task.group_folder,
                work_kind=InFlightWorkKind.SCHEDULED,
                input_messages=[{"sender_name": "System", "content": sample_task.prompt}],
                input_start_cursor="",
                input_end_cursor="",
                started_at="2026-07-25T10:00:00+00:00",
                task_id=sample_task.id,
                session_id="scheduled-thread",
                control_state=CheckpointControlState.PAUSED,
            )
        )

        with (
            patch("pynchy.host.orchestrator.task_scheduler.log_task_run") as log_run,
            patch("pynchy.host.orchestrator.task_scheduler.record_task_completion") as record,
            patch("pynchy.host.orchestrator.task_scheduler.update_task") as update,
            _patch_settings(groups_dir=tmp_path, poll_interval=0.01),
        ):
            assert await run_scheduled_agent(sample_task, mock_deps) is TurnOutcome.PAUSED

        assert sample_task.status == "active"
        assert mock_deps.agent_runs == []
        log_run.assert_not_called()
        record.assert_not_called()
        update.assert_not_called()
        paused = await get_in_flight_turn_for_task(sample_task.id)
        assert paused is not None
        assert paused.control_state is CheckpointControlState.PAUSED

    @pytest.mark.asyncio
    async def test_scheduled_reply_resume_reuses_session_and_finishes_original_occurrence(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        mock_deps.groups["test-jid"] = sample_group
        guidance_timestamp = "2026-07-25T10:05:00+00:00"
        await begin_in_flight_turn(
            InFlightTurn(
                turn_id="scheduled-resume",
                chat_jid=sample_task.chat_jid,
                group_folder=sample_task.group_folder,
                work_kind=InFlightWorkKind.SCHEDULED,
                input_messages=[
                    {
                        "message_type": "user",
                        "sender": "system",
                        "sender_name": "System",
                        "content": sample_task.prompt,
                        "timestamp": "2026-07-25T10:00:00+00:00",
                        "metadata": {"source": "scheduled_task"},
                    },
                    {
                        "message_type": "user",
                        "sender": "alice",
                        "sender_name": "Alice",
                        "content": "Resume without the finance section.",
                        "timestamp": guidance_timestamp,
                        "metadata": {"checkpoint_guidance": True},
                    },
                ],
                input_start_cursor="",
                input_end_cursor=guidance_timestamp,
                started_at="2026-07-25T10:00:00+00:00",
                task_id=sample_task.id,
                session_id="scheduled-provider-thread",
                input_source="scheduled_task",
            )
        )

        with (
            patch("pynchy.host.orchestrator.task_scheduler.log_task_run", new_callable=AsyncMock),
            patch(
                "pynchy.host.orchestrator.task_scheduler.record_task_completion",
                new_callable=AsyncMock,
            ),
            _patch_settings(groups_dir=tmp_path, poll_interval=0.01),
        ):
            assert await run_scheduled_agent(sample_task, mock_deps) is TurnOutcome.COMPLETED

        assert len(mock_deps.agent_runs) == 1
        resumed = mock_deps.agent_runs[0]
        assert resumed["turn_id"] == "scheduled-resume"
        assert resumed["resume_session_id"] == "scheduled-provider-thread"
        assert resumed["messages"][0]["metadata"]["source"] == "pause_continuation"
        assert resumed["messages"][1]["content"] == "Resume without the finance section."
        assert mock_deps.last_agent_timestamp[sample_task.chat_jid] == guidance_timestamp
        assert await get_in_flight_turn_for_task(sample_task.id) is None

    @pytest.mark.asyncio
    async def test_reused_workflow_id_keeps_distinct_temporal_runs_and_occurrences(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Recurring executions retain both Temporal and Pynchy identity boundaries."""
        mock_deps.groups["test-jid"] = sample_group
        await create_task(sample_task)
        temporal_runs = [
            _ActivityInfo(
                workflow_id="recurring-workflow",
                workflow_run_id="workflow-run-1",
                attempt=1,
            ),
            _ActivityInfo(
                workflow_id="recurring-workflow",
                workflow_run_id="workflow-run-2",
                attempt=1,
            ),
        ]

        with (
            patch.object(ts_mod.activity, "info", side_effect=temporal_runs),
            _patch_settings(groups_dir=tmp_path, poll_interval=0.01),
        ):
            assert await run_scheduled_agent(sample_task, mock_deps) is TurnOutcome.COMPLETED
            assert await run_scheduled_agent(sample_task, mock_deps) is TurnOutcome.COMPLETED

        logs = await get_task_run_logs(sample_task.id)
        assert [log.temporal_workflow_id for log in logs] == [
            "recurring-workflow",
            "recurring-workflow",
        ]
        assert [log.temporal_workflow_run_id for log in logs] == [
            "workflow-run-2",
            "workflow-run-1",
        ]
        assert logs[0].turn_id is not None
        assert logs[1].turn_id is not None
        assert logs[0].turn_id != logs[1].turn_id

    @pytest.mark.asyncio
    async def test_activity_retries_and_restart_continuation_share_turn_occurrence(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """One checkpoint groups retries and a distinct interrupted-turn workflow."""
        mock_deps.groups["test-jid"] = sample_group
        mock_deps._run_agent_result = "error"
        await create_task(sample_task)
        temporal_attempts = [
            _ActivityInfo(
                workflow_id="scheduled-workflow",
                workflow_run_id="scheduled-run",
                attempt=1,
            ),
            _ActivityInfo(
                workflow_id="scheduled-workflow",
                workflow_run_id="scheduled-run",
                attempt=2,
            ),
            _ActivityInfo(
                workflow_id="interrupted-workflow",
                workflow_run_id="interrupted-run",
                attempt=1,
            ),
        ]

        with (
            patch.object(ts_mod.activity, "info", side_effect=temporal_attempts),
            _patch_settings(groups_dir=tmp_path, poll_interval=0.01),
        ):
            assert await run_scheduled_agent(sample_task, mock_deps) is TurnOutcome.RETRY
            checkpoint = await get_in_flight_turn_for_task(sample_task.id)
            assert checkpoint is not None

            assert await run_scheduled_agent(sample_task, mock_deps) is TurnOutcome.RETRY
            await prepare_in_flight_turn_recovery("deploy-sha")
            mock_deps._run_agent_result = "success"
            assert await run_scheduled_agent(sample_task, mock_deps) is TurnOutcome.COMPLETED

        logs = list(reversed(await get_task_run_logs(sample_task.id)))
        assert [log.status for log in logs] == ["error", "error", "success"]
        assert [log.temporal_workflow_run_id for log in logs] == [
            "scheduled-run",
            "scheduled-run",
            "interrupted-run",
        ]
        assert [log.temporal_attempt for log in logs] == [1, 2, 1]
        assert {log.turn_id for log in logs} == {checkpoint.turn_id}

    @pytest.mark.asyncio
    async def test_scheduled_agent_honors_explicit_task_repo_access(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """A task-scoped repo reaches the public agent runner as an override."""
        mock_deps.groups["test-jid"] = sample_group
        sample_task.repo_access = "owner/pynchy"

        with patch("pynchy.host.orchestrator.task_scheduler.log_task_run", new_callable=AsyncMock):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.record_task_completion",
                new_callable=AsyncMock,
            ):
                with patch(
                    "pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock
                ):
                    with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
                        await _run_due_task_via_scheduler(mock_deps, sample_task)

        assert len(mock_deps.agent_runs) == 1
        assert mock_deps.agent_runs[0]["repo_access_override"] == "owner/pynchy"

    @pytest.mark.asyncio
    async def test_scheduled_agent_without_task_repo_uses_workspace_defaults(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """An absent task scope preserves the runner's workspace-default behavior."""
        mock_deps.groups["test-jid"] = sample_group

        with (
            patch("pynchy.host.orchestrator.task_scheduler.log_task_run", new_callable=AsyncMock),
            patch(
                "pynchy.host.orchestrator.task_scheduler.record_task_completion",
                new_callable=AsyncMock,
            ),
            patch("pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock),
            _patch_settings(groups_dir=tmp_path, poll_interval=0.01),
        ):
            await _run_due_task_via_scheduler(mock_deps, sample_task)

        assert len(mock_deps.agent_runs) == 1
        assert mock_deps.agent_runs[0]["repo_access_override"] is None

    @pytest.mark.asyncio
    async def test_sends_start_notification(self, mock_deps, sample_task, sample_group, tmp_path):
        """Should broadcast start notification before running agent."""
        mock_deps.groups["test-jid"] = sample_group

        with patch("pynchy.host.orchestrator.task_scheduler.log_task_run", new_callable=AsyncMock):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.record_task_completion",
                new_callable=AsyncMock,
            ):
                with patch(
                    "pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock
                ):
                    with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
                        await _run_due_task_via_scheduler(mock_deps, sample_task)

        # Check that a scheduled task starting event was broadcast
        assert len(mock_deps.messages) >= 1
        jid, event = mock_deps.messages[0]
        assert jid == "test@g.us"
        assert "\u23f1 Scheduled task starting." in event.content
