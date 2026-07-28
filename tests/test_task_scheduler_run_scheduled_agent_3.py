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
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from conftest import (
    configure_workspace_placement_for,
    make_settings,
)

from pynchy.agent_protocol.api import (
    ContainerOutput,
    InFlightTurn,
    InFlightWorkKind,
)
from pynchy.config.api import JobConfig, ProfileConfig, WorkspaceConfig
from pynchy.host.orchestrator.host_shell import ShellResult
from pynchy.host.orchestrator.task_scheduler import run_scheduled_agent
from pynchy.scheduling.api import (
    ScheduledTask,
    SessionPolicy,
)
from pynchy.state import (
    begin_in_flight_turn,
    create_task,
    get_in_flight_turn_for_task,
    get_task_by_id,
    init_test_database,
)
from pynchy.turn_outcomes import TurnOutcome
from pynchy.workspace.api import (
    WorkspaceProfile,
)
from tests.task_scheduler_support import (
    _configure_scheduler_runtime,
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
    async def test_deterministic_scoped_job_publishes_to_bound_thread_without_agent(
        self, mock_deps, tmp_path
    ):
        settings = make_settings(
            groups_dir=tmp_path,
            profiles={
                "category": ProfileConfig(),
                "cron": ProfileConfig(skills=["scheduler-operations"]),
            },
            workspaces={
                "ops": WorkspaceConfig(
                    profiles=["category"],
                    scopes=[{"workspace": "cron", "profiles": ["cron"]}],
                )
            },
            jobs={
                "watchdog": JobConfig(
                    schedule="0 23 * * *",
                    workspace="cron",
                    agent=False,
                    command="scripts/watchdog.py",
                )
            },
        )
        mock_deps.groups = {
            "discord:channel:ops": WorkspaceProfile(
                jid="discord:channel:ops",
                name="Ops",
                folder="ops",
                trigger="@Pynchy",
            ),
            "discord:channel:watchdog-runtime": WorkspaceProfile(
                jid="discord:channel:watchdog-runtime",
                name="Ops/cron | watchdog",
                folder="cron-runtime",
                trigger="@Pynchy",
            ),
        }
        task = ScheduledTask(
            id="watchdog-task",
            group_folder="cron",
            chat_jid="discord:channel:ops",
            prompt="",
            schedule_type="cron",
            schedule_value="0 23 * * *",
            session_policy=SessionPolicy.RESET_BEFORE_RUN,
            config_job_name="watchdog",
            config_job_is_deterministic=True,
            config_job_command="scripts/watchdog.py",
            config_job_cwd=str(tmp_path),
            derived_thread_name="cron | watchdog",
            bound_chat_jid="discord:channel:watchdog-runtime",
            bound_group_folder="cron-runtime",
        )

        configure_workspace_placement_for(settings)
        _configure_scheduler_runtime(mock_deps, settings)
        with (
            patch(
                "pynchy.host.orchestrator.config_job_execution.run_shell_command",
                new_callable=AsyncMock,
                return_value=ShellResult(returncode=0, stdout="watchdog ok", stderr=""),
            ),
            patch(
                "pynchy.host.orchestrator.task_scheduler.get_task_run_logs",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("pynchy.host.orchestrator.task_scheduler.log_task_run", new_callable=AsyncMock),
            patch(
                "pynchy.host.orchestrator.task_scheduler.record_task_completion",
                new_callable=AsyncMock,
            ),
        ):
            assert await run_scheduled_agent(task, mock_deps) is TurnOutcome.COMPLETED

        assert mock_deps.agent_runs == []
        assert mock_deps.thread_creations == []
        assert mock_deps.host_messages == [("discord:channel:watchdog-runtime", "watchdog ok")]

    @pytest.mark.asyncio
    async def test_pre_run_false_gate_skips_agent_in_existing_binding(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        sample_task.config_job_name = "gated-job"
        sample_task.config_job_pre_run_command = "scripts/gate.py"
        sample_task.config_job_pre_run_cwd = str(tmp_path)
        sample_task.derived_thread_name = "test-group | gated-job"
        mock_deps.groups["test-jid"] = sample_group
        settings = make_settings(
            groups_dir=tmp_path,
            profiles={"test": ProfileConfig()},
            workspaces={"test-group": WorkspaceConfig(profiles=["test"])},
            jobs={
                "gated-job": JobConfig(
                    schedule="0 8 * * *",
                    workspace="test-group",
                    prompt="Act only when needed.",
                    pre_run_command="scripts/gate.py",
                )
            },
        )

        _configure_scheduler_runtime(mock_deps, settings)
        with (
            patch(
                "pynchy.host.orchestrator.config_job_execution.run_shell_command",
                new_callable=AsyncMock,
                return_value=ShellResult(
                    returncode=0,
                    stdout='{"wakeAgent": false}',
                    stderr="",
                ),
            ),
            patch(
                "pynchy.host.orchestrator.task_scheduler.get_task_run_logs",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("pynchy.host.orchestrator.task_scheduler.log_task_run", new_callable=AsyncMock),
            patch(
                "pynchy.host.orchestrator.task_scheduler.record_task_completion",
                new_callable=AsyncMock,
            ),
        ):
            assert await run_scheduled_agent(sample_task, mock_deps) is TurnOutcome.COMPLETED

        assert mock_deps.thread_lookups == []
        assert mock_deps.thread_creations == []
        assert mock_deps.agent_runs == []

    @pytest.mark.asyncio
    async def test_config_job_worker_restart_does_not_replace_its_binding(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Worker disposal is independent from durable thread ownership."""
        mock_deps.groups["test-jid"] = sample_group
        sample_task.config_job_name = "fam_daily_checkin"

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
            await _run_due_task_via_scheduler(mock_deps, sample_task)

        assert mock_deps.thread_creations == []
        assert [run["chat_jid"] for run in mock_deps.agent_runs] == [
            "test@g.us",
            "test@g.us",
        ]

    @pytest.mark.asyncio
    async def test_config_job_rename_does_not_replace_runtime_during_execution(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        mock_deps.groups["test-jid"] = sample_group
        sample_task.config_job_name = "fam_daily_checkin"

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
            sample_task.config_job_name = "family_daily_checkin"
            await _run_due_task_via_scheduler(mock_deps, sample_task)

        assert mock_deps.thread_creations == []
        assert [run["chat_jid"] for run in mock_deps.agent_runs] == [
            "test@g.us",
            "test@g.us",
        ]

    @pytest.mark.asyncio
    async def test_config_jobs_in_one_workspace_run_concurrently_in_separate_threads(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Distinct job names do not serialize through their shared root."""
        mock_deps.groups["test-jid"] = sample_group
        mock_deps.groups["second-jid"] = WorkspaceProfile(
            jid="second@g.us",
            name="Test Group/second",
            folder="test-group-second",
            trigger="@bot",
        )
        sample_task.config_job_name = "fam_daily_checkin"
        second_task = ScheduledTask(
            id="task-2",
            group_folder=sample_task.group_folder,
            chat_jid=sample_task.chat_jid,
            prompt="Tend the garden.",
            schedule_type=sample_task.schedule_type,
            schedule_value=sample_task.schedule_value,
            session_policy=sample_task.session_policy,
            config_job_name="fam_gardener",
            bound_chat_jid="second@g.us",
            bound_group_folder="test-group-second",
        )
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def mock_run(_group, chat_jid, _messages, _on_output, **_kwargs):
            if chat_jid == "test@g.us":
                first_started.set()
                await release_first.wait()
            return "success"

        mock_deps._run_agent_side_effect = mock_run

        with (
            patch(
                "pynchy.host.orchestrator.task_scheduler.get_task_run_logs",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("pynchy.host.orchestrator.task_scheduler.log_task_run", new_callable=AsyncMock),
            patch(
                "pynchy.host.orchestrator.task_scheduler.record_task_completion",
                new_callable=AsyncMock,
            ),
            patch("pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock),
            _patch_settings(groups_dir=tmp_path, poll_interval=0.01),
        ):
            first_run = asyncio.create_task(run_scheduled_agent(sample_task, mock_deps))
            await first_started.wait()
            await run_scheduled_agent(second_task, mock_deps)
            release_first.set()
            await first_run

        assert mock_deps.thread_creations == []
        assert [run["chat_jid"] for run in mock_deps.agent_runs] == [
            "test@g.us",
            "second@g.us",
        ]

    @pytest.mark.asyncio
    async def test_config_job_overlap_serializes_in_its_one_task_thread(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Duplicate delivery waits rather than creating a spillover target."""
        mock_deps.groups["test-jid"] = sample_group
        sample_task.config_job_name = "fam_daily_checkin"
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        run_count = 0

        async def mock_run(_group, _chat_jid, _messages, _on_output, **_kwargs):
            nonlocal run_count
            run_count += 1
            if run_count == 1:
                first_started.set()
                await release_first.wait()
            return "success"

        mock_deps._run_agent_side_effect = mock_run

        with (
            patch(
                "pynchy.host.orchestrator.task_scheduler.get_task_run_logs",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("pynchy.host.orchestrator.task_scheduler.log_task_run", new_callable=AsyncMock),
            patch(
                "pynchy.host.orchestrator.task_scheduler.record_task_completion",
                new_callable=AsyncMock,
            ),
            patch("pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock),
            _patch_settings(groups_dir=tmp_path, poll_interval=0.01),
        ):
            first_run = asyncio.create_task(run_scheduled_agent(sample_task, mock_deps))
            await first_started.wait()
            second_run = asyncio.create_task(run_scheduled_agent(sample_task, mock_deps))
            await asyncio.sleep(0)
            assert run_count == 1
            release_first.set()
            await asyncio.gather(first_run, second_run)

        assert mock_deps.thread_creations == []
        assert [run["chat_jid"] for run in mock_deps.agent_runs] == [
            "test@g.us",
            "test@g.us",
        ]

    @pytest.mark.asyncio
    async def test_config_job_retry_reuses_its_checkpointed_thread(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """An activity retry resumes the run checkpoint without task-level thread state."""
        mock_deps.groups["test-jid"] = sample_group
        sample_task.config_job_name = "fam_daily_checkin"
        await create_task(sample_task)
        mock_deps._run_agent_result = "error"

        with (
            patch("pynchy.host.orchestrator.task_scheduler.log_task_run", new_callable=AsyncMock),
            patch(
                "pynchy.host.orchestrator.task_scheduler.record_task_completion",
                new_callable=AsyncMock,
            ),
            patch("pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock),
            _patch_settings(groups_dir=tmp_path, poll_interval=0.01),
        ):
            assert await run_scheduled_agent(sample_task, mock_deps) is TurnOutcome.RETRY
            reloaded_task = await get_task_by_id(sample_task.id)
            assert reloaded_task is not None
            assert reloaded_task is not sample_task
            assert reloaded_task.config_job_name == "fam_daily_checkin"
            lookups_before_retry = len(mock_deps.thread_lookups)
            creations_before_retry = len(mock_deps.thread_creations)
            mock_deps._run_agent_result = "success"
            assert await run_scheduled_agent(reloaded_task, mock_deps) is TurnOutcome.COMPLETED

        assert mock_deps.thread_creations == []
        assert len(mock_deps.thread_lookups) == lookups_before_retry
        assert len(mock_deps.thread_creations) == creations_before_retry
        assert [run["chat_jid"] for run in mock_deps.agent_runs] == [
            "test@g.us",
            "test@g.us",
        ]

    @pytest.mark.asyncio
    async def test_resumed_task_uses_runtime_current_chat_binding(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Deploy recovery keeps stable runtime identity after its JID is replaced."""
        checkpoint_jid = "discord:channel:scheduled-old"
        current_jid = "discord:channel:scheduled-current"
        child_folder = "test-group__thread_discord-channel-scheduled-1"
        current_group = replace(
            sample_group,
            jid=current_jid,
            folder=child_folder,
        )
        mock_deps.groups[current_jid] = current_group
        sample_task = replace(
            sample_task,
            bound_chat_jid=current_jid,
            bound_group_folder=child_folder,
        )
        await begin_in_flight_turn(
            InFlightTurn(
                turn_id="scheduled-turn",
                chat_jid=checkpoint_jid,
                group_folder=child_folder,
                work_kind=InFlightWorkKind.SCHEDULED,
                input_messages=[{"content": sample_task.prompt}],
                input_start_cursor="",
                input_end_cursor="",
                started_at=datetime.now(UTC).isoformat(),
                task_id=sample_task.id,
            )
        )

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

        assert mock_deps.thread_creations == []
        assert mock_deps.agent_runs[0]["chat_jid"] == current_jid
        assert mock_deps.agent_runs[0]["group"].folder == child_folder

    @pytest.mark.asyncio
    async def test_retry_reuses_bound_runtime_after_agent_error(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """A retry resumes the checkpoint in the task's durable runtime."""
        mock_deps.groups["test-jid"] = sample_group
        await begin_in_flight_turn(
            InFlightTurn(
                turn_id="human-turn",
                chat_jid=sample_task.chat_jid,
                group_folder=sample_task.group_folder,
                work_kind=InFlightWorkKind.INTERACTIVE,
                input_messages=[{"content": "Keep working."}],
                input_start_cursor="",
                input_end_cursor="",
                started_at=datetime.now(UTC).isoformat(),
            )
        )
        mock_deps._run_agent_result = "error"

        with (
            patch("pynchy.host.orchestrator.task_scheduler.log_task_run", new_callable=AsyncMock),
            patch(
                "pynchy.host.orchestrator.task_scheduler.record_task_completion",
                new_callable=AsyncMock,
            ),
            patch("pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock),
            _patch_settings(groups_dir=tmp_path, poll_interval=0.01),
        ):
            assert await run_scheduled_agent(sample_task, mock_deps) is TurnOutcome.RETRY
            checkpoint = await get_in_flight_turn_for_task(sample_task.id)
            assert checkpoint is not None

            mock_deps._run_agent_result = "success"
            assert await run_scheduled_agent(sample_task, mock_deps) is TurnOutcome.COMPLETED

        assert mock_deps.thread_creations == []
        assert [run["chat_jid"] for run in mock_deps.agent_runs] == [
            "test@g.us",
            "test@g.us",
        ]
        assert await get_in_flight_turn_for_task(sample_task.id) is None

    @pytest.mark.asyncio
    async def test_on_output_delegates_to_handle_streamed_output(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Should delegate streamed output to deps.handle_streamed_output."""
        mock_deps.groups["test-jid"] = sample_group
        streamed = ContainerOutput(status="success", result="Task completed")

        async def mock_run(group, chat_jid, messages, on_output, **kwargs):
            if on_output:
                await on_output(streamed)
            return "success"

        mock_deps._run_agent_side_effect = mock_run

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

        # Should have delegated to handle_streamed_output
        assert len(mock_deps.streamed_outputs) == 1
        assert mock_deps.streamed_outputs[0][0] == "test@g.us"
        assert mock_deps.streamed_outputs[0][3] == mock_deps.agent_runs[0]["turn_id"]

    @pytest.mark.asyncio
    async def test_records_completion_without_calculating_next_run_for_cron_schedule(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Recurring execution records evidence without rewriting schedule timing."""
        mock_deps.groups["test-jid"] = sample_group
        sample_task.schedule_type = "cron"
        sample_task.schedule_value = "0 9 * * *"  # Daily at 9am

        completions = []

        def mock_record(task_id, *, last_result, completed):
            completions.append((task_id, last_result, completed))

        with patch("pynchy.host.orchestrator.task_scheduler.log_task_run", new_callable=AsyncMock):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.record_task_completion",
                side_effect=mock_record,
            ):
                with patch(
                    "pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock
                ):
                    with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
                        await _run_due_task_via_scheduler(mock_deps, sample_task)

        assert completions == [("task-1", "Completed", False)]

    @pytest.mark.asyncio
    async def test_records_completion_without_calculating_next_run_for_interval_schedule(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Interval timing remains Temporal-owned after a run."""
        mock_deps.groups["test-jid"] = sample_group
        sample_task.schedule_type = "interval"
        sample_task.schedule_value = "300000"  # 5 minutes in ms

        completions = []

        def mock_record(task_id, *, last_result, completed):
            completions.append((task_id, last_result, completed))

        with patch("pynchy.host.orchestrator.task_scheduler.log_task_run", new_callable=AsyncMock):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.record_task_completion",
                side_effect=mock_record,
            ):
                with patch(
                    "pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock
                ):
                    with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
                        await _run_due_task_via_scheduler(mock_deps, sample_task)

        assert completions == [("task-1", "Completed", False)]

    @pytest.mark.asyncio
    async def test_marks_once_schedule_completed_without_calculating_next_run(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """A one-shot completion only records its terminal application state."""
        mock_deps.groups["test-jid"] = sample_group
        sample_task.schedule_type = "once"
        sample_task.schedule_value = "2024-12-31T23:59:59"

        completions = []

        def mock_record(task_id, *, last_result, completed):
            completions.append((task_id, last_result, completed))

        with patch("pynchy.host.orchestrator.task_scheduler.log_task_run", new_callable=AsyncMock):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.record_task_completion",
                side_effect=mock_record,
            ):
                with patch(
                    "pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock
                ):
                    with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
                        await _run_due_task_via_scheduler(mock_deps, sample_task)

        assert completions == [("task-1", "Completed", True)]
