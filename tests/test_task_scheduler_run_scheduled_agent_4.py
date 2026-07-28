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
from contextvars import ContextVar
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.agent_protocol.api import (
    ContainerOutput,
)
from pynchy.config.api import JobConfig
from pynchy.host.orchestrator.scheduler_deps import (
    ScheduledExecutionLifecycle,
)
from pynchy.host.orchestrator.task_scheduler import run_scheduled_agent
from pynchy.state import (
    init_test_database,
)
from pynchy.turn_outcomes import TurnOutcome
from pynchy.work_items.api import WorkItemExecutionStatus
from tests.task_scheduler_support import (
    MockSchedulerDeps,
    _patch_settings,
    _run_due_task_via_scheduler,
    _run_scheduler_reconcile_once,
)

pytest_plugins = ("tests.task_scheduler_support",)

if TYPE_CHECKING:
    from pynchy.scheduling.api import (
        TaskRunLog,
    )

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
    @pytest.mark.parametrize(
        ("execution_status", "expected_run_status", "expected_result"),
        [
            (
                WorkItemExecutionStatus.IN_PROGRESS,
                "incomplete",
                "Incomplete: Agent response",
            ),
            (
                WorkItemExecutionStatus.AWAITING_REVIEW,
                "success",
                "Agent response",
            ),
        ],
        ids=["still-in-progress", "explicit-outcome"],
    )
    async def test_linear_execution_requires_explicit_lifecycle_outcome(
        self,
        mock_deps,
        sample_task,
        sample_group,
        tmp_path,
        execution_status,
        expected_run_status,
        expected_result,
    ):
        """A clean Linear run succeeds only after recording a lifecycle outcome."""
        mock_deps.groups["test-jid"] = sample_group
        sample_task.schedule_type = "once"

        async def agent_response(_group, _jid, _messages, on_output, **_kwargs):
            await on_output(ContainerOutput(status="success", result="Agent response"))
            return "success"

        mock_deps._run_agent_side_effect = agent_response
        mock_deps.scheduled_execution = ScheduledExecutionLifecycle(
            execution_id="execution-1",
            status=execution_status.value,
            has_explicit_outcome=execution_status.is_explicit_lifecycle_outcome,
        )
        logged_runs = []
        completions = []

        def mock_record(task_id, *, last_result, completed):
            completions.append((task_id, last_result, completed))

        with (
            patch(
                "pynchy.host.orchestrator.task_scheduler.log_task_run",
                side_effect=logged_runs.append,
            ),
            patch(
                "pynchy.host.orchestrator.task_scheduler.record_task_completion",
                side_effect=mock_record,
            ),
            _patch_settings(groups_dir=tmp_path, poll_interval=0.01),
        ):
            completed = await run_scheduled_agent(sample_task, mock_deps)

        assert completed is TurnOutcome.COMPLETED
        assert [run.status for run in logged_runs] == [expected_run_status]
        assert completions == [("task-1", expected_result, True)]
        assert mock_deps.scheduled_execution_queries == ["task-1"]

    @pytest.mark.asyncio
    async def test_once_schedule_remains_active_for_temporal_retry_after_agent_error(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """A failed one-shot must remain runnable for the workflow retry."""
        mock_deps.groups["test-jid"] = sample_group
        mock_deps._run_agent_result = "error"
        sample_task.schedule_type = "once"
        sample_task.schedule_value = "2026-07-19T08:00:00+00:00"
        completions = []

        def mock_record(task_id, *, last_result, completed):
            completions.append((task_id, last_result, completed))

        with patch("pynchy.host.orchestrator.task_scheduler.log_task_run", new_callable=AsyncMock):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.record_task_completion",
                side_effect=mock_record,
            ):
                with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
                    completed = await run_scheduled_agent(sample_task, mock_deps)

        assert completed is TurnOutcome.RETRY
        assert completions == [("task-1", "Error: Agent returned error", False)]

    @pytest.mark.asyncio
    async def test_logs_error_on_agent_exception(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Should log error when run_agent raises an exception."""
        mock_deps.groups["test-jid"] = sample_group

        def mock_run_raise(group, chat_jid, messages, on_output, **kwargs):
            raise ValueError(AGENT_FAILED_MESSAGE)

        mock_deps._run_agent_side_effect = mock_run_raise

        logged_runs = []

        def mock_log_run(log: TaskRunLog):
            logged_runs.append(log)

        with patch(
            "pynchy.host.orchestrator.task_scheduler.log_task_run", side_effect=mock_log_run
        ):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.record_task_completion",
                new_callable=AsyncMock,
            ):
                with patch(
                    "pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock
                ):
                    with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
                        await _run_due_task_via_scheduler(mock_deps, sample_task)

        # Should have logged the error
        assert len(logged_runs) == 1
        assert logged_runs[0].status == "error"
        assert "Agent failed" in logged_runs[0].error

    @pytest.mark.asyncio
    async def test_logs_error_on_agent_error_return(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Should log error when run_agent returns 'error'."""
        mock_deps.groups["test-jid"] = sample_group
        mock_deps._run_agent_result = "error"

        logged_runs = []

        def mock_log_run(log: TaskRunLog):
            logged_runs.append(log)

        with patch(
            "pynchy.host.orchestrator.task_scheduler.log_task_run", side_effect=mock_log_run
        ):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.record_task_completion",
                new_callable=AsyncMock,
            ):
                with patch(
                    "pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock
                ):
                    with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
                        await _run_due_task_via_scheduler(mock_deps, sample_task)

        # Should have logged the error
        assert len(logged_runs) == 1
        assert logged_runs[0].status == "error"
        assert "Agent returned error" in logged_runs[0].error

    @pytest.mark.asyncio
    async def test_does_not_update_next_run_before_execution(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Temporal overlap handling removes the local next-run guard."""
        mock_deps.groups["test-jid"] = sample_group
        sample_task.schedule_type = "cron"
        sample_task.schedule_value = "0 4 * * *"

        early_updates = []

        def mock_update(task_id, updates):
            early_updates.append((task_id, updates))

        with patch("pynchy.host.orchestrator.task_scheduler.log_task_run", new_callable=AsyncMock):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.record_task_completion",
                new_callable=AsyncMock,
            ):
                with patch(
                    "pynchy.host.orchestrator.task_scheduler.update_task", side_effect=mock_update
                ):
                    with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
                        await _run_due_task_via_scheduler(mock_deps, sample_task)

        assert early_updates == []

    @pytest.mark.asyncio
    async def test_detects_error_status_from_container(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Should classify streamed status='error' outputs (e.g. SDK is_error) as task errors."""
        mock_deps.groups["test-jid"] = sample_group
        api_error_text = (
            'API Error: 429 {"error":{"type":"rate_limit_error","message":'
            '"This request would exceed your account\'s rate limit."}}'
        )
        # Container now emits status="error" when ResultMessage.is_error is True
        streamed = ContainerOutput(status="error", error=api_error_text)

        def mock_run(group, chat_jid, messages, on_output, **kwargs):
            async def _run():
                if on_output:
                    await on_output(streamed)
                return "success"

            return _run()

        mock_deps._run_agent_side_effect = mock_run

        logged_runs = []

        def mock_log_run(log: TaskRunLog):
            logged_runs.append(log)

        with patch(
            "pynchy.host.orchestrator.task_scheduler.log_task_run", side_effect=mock_log_run
        ):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.record_task_completion",
                new_callable=AsyncMock,
            ):
                with patch(
                    "pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock
                ):
                    with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
                        await _run_due_task_via_scheduler(mock_deps, sample_task)

        assert len(logged_runs) == 1
        assert logged_runs[0].status == "error"
        assert "API Error: 429" in logged_runs[0].error


class TestHostJobs:
    @pytest.mark.asyncio
    async def test_configured_host_cron_jobs_are_not_spawned_by_scheduler(
        self, tmp_path, monkeypatch
    ):
        with (
            _patch_settings(
                jobs={
                    "rebuild_container": JobConfig(
                        schedule="0 5 * * *",
                        workspace="host",
                        command="./src/pynchy/agent/build.sh",
                    )
                },
            ),
            patch(
                "pynchy.host.orchestrator.task_scheduler.asyncio.create_subprocess_shell",
                new_callable=AsyncMock,
            ) as mock_spawn,
        ):
            runtime_cls = await _run_scheduler_reconcile_once(MockSchedulerDeps())

        mock_spawn.assert_not_awaited()
        assert runtime_cls.instances[0].reconcile_count == 1

    @pytest.mark.asyncio
    async def test_skips_disabled_host_cron_job(self, monkeypatch):
        with (
            _patch_settings(
                jobs={
                    "disabled_job": JobConfig(
                        schedule="0 5 * * *",
                        workspace="host",
                        command="echo hello",
                        enabled=False,
                    )
                },
            ),
            patch(
                "pynchy.host.orchestrator.task_scheduler.asyncio.create_subprocess_shell",
                new_callable=AsyncMock,
            ) as mock_spawn,
        ):
            runtime_cls = await _run_scheduler_reconcile_once(MockSchedulerDeps())

        mock_spawn.assert_not_awaited()
        assert runtime_cls.instances[0].reconcile_count == 1
