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

import asyncio
from contextvars import ContextVar
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
from pynchy.conversation.api import dynamic_thread_folder
from pynchy.host.orchestrator.task_scheduler import run_scheduled_agent
from pynchy.identifiers import RuntimeId
from pynchy.scheduling.api import (
    ScheduledTask,
    SessionPolicy,
)
from pynchy.state import (
    begin_in_flight_turn,
    get_in_flight_turn_for_task,
    init_test_database,
)
from pynchy.turn_outcomes import TurnOutcome
from pynchy.workspace.api import (
    RuntimeTarget,
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
    async def test_uses_bound_runtime_when_an_old_human_checkpoint_exists(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Checkpoint history never causes a scheduled occurrence to fork runtime ownership."""
        mock_deps.groups["test-jid"] = sample_group
        await begin_in_flight_turn(
            InFlightTurn(
                turn_id="human-turn",
                chat_jid=sample_task.chat_jid,
                group_folder=sample_task.group_folder,
                work_kind=InFlightWorkKind.INTERACTIVE,
                input_messages=[{"sender": "123456", "content": "Please investigate this."}],
                input_start_cursor="",
                input_end_cursor="",
                started_at=datetime.now(UTC).isoformat(),
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
        scheduled_run = mock_deps.agent_runs[0]
        assert scheduled_run["chat_jid"] == "test@g.us"
        assert scheduled_run["group"].folder == "test-group"
        assert mock_deps.messages[0][0] == "test@g.us"

    @pytest.mark.asyncio
    async def test_ignores_worker_liveness_when_runtime_is_already_bound(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Worker liveness does not change durable destination ownership."""
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

        assert mock_deps.thread_creations == []
        assert mock_deps.agent_runs[0]["chat_jid"] == "test@g.us"

    @pytest.mark.asyncio
    async def test_other_thread_checkpoints_do_not_retarget_the_bound_runtime(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Unrelated checkpoints cannot replace a task's persisted runtime owner."""
        mock_deps.groups["test-jid"] = sample_group
        existing_jid = "discord:channel:existing-1"
        mock_deps.existing_threads["test-group-1"] = existing_jid
        await begin_in_flight_turn(
            InFlightTurn(
                turn_id="human-turn",
                chat_jid=sample_task.chat_jid,
                group_folder=sample_task.group_folder,
                work_kind=InFlightWorkKind.INTERACTIVE,
                input_messages=[{"content": "Please investigate this."}],
                input_start_cursor="",
                input_end_cursor="",
                started_at=datetime.now(UTC).isoformat(),
            )
        )
        await begin_in_flight_turn(
            InFlightTurn(
                turn_id="child-turn",
                chat_jid=existing_jid,
                group_folder=dynamic_thread_folder(sample_task.group_folder, existing_jid),
                work_kind=InFlightWorkKind.INTERACTIVE,
                input_messages=[{"content": "Working in the child thread."}],
                input_start_cursor="",
                input_end_cursor="",
                started_at=datetime.now(UTC).isoformat(),
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

        assert mock_deps.thread_lookups == []
        assert mock_deps.thread_creations == []
        assert mock_deps.agent_runs[0]["chat_jid"] == "test@g.us"

    @pytest.mark.asyncio
    async def test_thread_queue_serializes_two_scheduled_tasks_in_one_runtime(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """One thread queue prevents two workers while both tasks reuse its runtime."""
        mock_deps.groups["test-jid"] = sample_group
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def mock_run(_group, chat_jid, _messages, _on_output, **_kwargs):
            if chat_jid == sample_task.chat_jid:
                first_started.set()
                await release_first.wait()
            return "success"

        mock_deps._run_agent_side_effect = mock_run
        second_task = ScheduledTask(
            id="task-2",
            group_folder=sample_task.group_folder,
            chat_jid=sample_task.chat_jid,
            prompt="Second task",
            schedule_type=sample_task.schedule_type,
            schedule_value=sample_task.schedule_value,
            session_policy=sample_task.session_policy,
            bound_chat_jid=sample_task.bound_chat_jid,
            bound_group_folder=sample_task.bound_group_folder,
            status=sample_task.status,
        )

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
            first_run = asyncio.create_task(
                mock_deps.queue.run_serialized_task(
                    RuntimeTarget.from_binding(
                        sample_group.folder,
                        sample_task.bound_chat_jid or sample_task.chat_jid,
                    ),
                    sample_task.id,
                    lambda: run_scheduled_agent(sample_task, mock_deps),
                )
            )
            await first_started.wait()
            second_run = asyncio.create_task(
                mock_deps.queue.run_serialized_task(
                    RuntimeTarget.from_binding(
                        sample_group.folder,
                        second_task.bound_chat_jid or second_task.chat_jid,
                    ),
                    second_task.id,
                    lambda: run_scheduled_agent(second_task, mock_deps),
                )
            )
            await asyncio.sleep(0)
            assert len(mock_deps.agent_runs) == 1
            release_first.set()
            await asyncio.gather(first_run, second_run)

        assert mock_deps.thread_creations == []
        assert [run["chat_jid"] for run in mock_deps.agent_runs] == [
            "test@g.us",
            "test@g.us",
        ]

    @pytest.mark.asyncio
    async def test_interactive_turn_interrupts_schedule_at_tool_boundary_then_runs(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Human input runs next while the scheduled checkpoint remains resumable."""
        mock_deps.groups[sample_group.jid] = sample_group
        interactive_ran = asyncio.Event()

        async def process_messages(_chat_jid: str) -> TurnOutcome:
            interactive_ran.set()
            await asyncio.sleep(0)
            return TurnOutcome.COMPLETED

        mock_deps.queue.set_process_messages_fn(process_messages)

        async def scheduled_run(_group, _chat_jid, _messages, on_output, **_kwargs):
            mock_deps.queue.defer_interrupt_until_tool_result(RuntimeId(sample_group.folder))
            await on_output(
                ContainerOutput(
                    type="tool_result",
                    status="success",
                    result="checkpointed tool completed",
                )
            )
            return "success"

        mock_deps._run_agent_side_effect = scheduled_run

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
            completed = await mock_deps.queue.run_serialized_task(
                RuntimeTarget.from_binding(
                    sample_group.folder,
                    sample_task.bound_chat_jid or sample_task.chat_jid,
                ),
                sample_task.id,
                lambda: run_scheduled_agent(
                    sample_task,
                    mock_deps,
                    occurrence_id="occurrence-1",
                ),
            )
            await interactive_ran.wait()

        assert completed is TurnOutcome.RETRY
        checkpoint = await get_in_flight_turn_for_task(sample_task.id)
        assert checkpoint is not None
        assert checkpoint.output_sent is True
        assert len(mock_deps.agent_runs) == 1

    @pytest.mark.asyncio
    async def test_unrelated_failed_checkpoint_does_not_create_an_ephemeral_runtime(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """A stale sibling checkpoint cannot make this task fork to another runtime."""
        mock_deps.groups["test-jid"] = sample_group
        mock_deps.thread_creation_supported = False
        await begin_in_flight_turn(
            InFlightTurn(
                turn_id="prior-failed-turn",
                chat_jid=sample_task.chat_jid,
                group_folder=sample_task.group_folder,
                work_kind=InFlightWorkKind.SCHEDULED,
                input_messages=[{"content": "Earlier weekly task"}],
                input_start_cursor="",
                input_end_cursor="",
                started_at=datetime.now(UTC).isoformat(),
                task_id="prior-task",
            )
        )
        log_task_run = AsyncMock()
        record_task_completion = AsyncMock()

        with (
            patch(
                "pynchy.host.orchestrator.task_scheduler.get_task_run_logs",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "pynchy.host.orchestrator.task_scheduler.log_task_run",
                new=log_task_run,
            ),
            patch(
                "pynchy.host.orchestrator.task_scheduler.record_task_completion",
                new=record_task_completion,
            ),
            patch("pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock),
            _patch_settings(groups_dir=tmp_path, poll_interval=0.01),
        ):
            await run_scheduled_agent(sample_task, mock_deps)

        assert mock_deps.thread_lookups == []
        assert mock_deps.thread_creations == []
        assert mock_deps.agent_runs[0]["chat_jid"] == "test@g.us"
        assert await get_in_flight_turn_for_task("prior-task") is not None
        assert await get_in_flight_turn_for_task(sample_task.id) is None
        log_task_run.assert_awaited_once()
        record_task_completion.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_config_job_reuses_its_persisted_binding_on_each_run(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Thread creation is a binding concern, not a per-occurrence lifecycle."""
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

        assert mock_deps.thread_lookups == []
        assert mock_deps.thread_creations == []
        assert [run["chat_jid"] for run in mock_deps.agent_runs] == [
            "test@g.us",
            "test@g.us",
        ]
        assert [run["group"].folder for run in mock_deps.agent_runs] == [
            "test-group",
            "test-group",
        ]

    @pytest.mark.asyncio
    async def test_scoped_jobs_use_owner_policy_under_category_parent(self, mock_deps, tmp_path):
        settings = make_settings(
            groups_dir=tmp_path,
            profiles={
                "category": ProfileConfig(),
                "fam": ProfileConfig(repo="crypdick/fam"),
                "pynchy-dev": ProfileConfig(
                    repo="crypdick/pynchy",
                    execution_mode="host",
                    cwd="/srv/pynchy",
                    is_admin=True,
                ),
            },
            workspaces={
                "relationships": WorkspaceConfig(
                    profiles=["category"],
                    scopes=[{"workspace": "fam", "profiles": ["fam"]}],
                ),
                "admin": WorkspaceConfig(
                    profiles=["category"],
                    scopes=[{"workspace": "pynchy-dev", "profiles": ["pynchy-dev"]}],
                ),
            },
            jobs={
                "fam-check": JobConfig(schedule="0 8 * * *", workspace="fam", prompt="Check fam."),
                "pynchy-check": JobConfig(
                    schedule="0 9 * * *",
                    workspace="pynchy-dev",
                    prompt="Check Pynchy.",
                ),
            },
        )
        mock_deps.groups = {
            "discord:channel:relationships": WorkspaceProfile(
                jid="discord:channel:relationships",
                name="Relationships",
                folder="relationships",
                trigger="@Pynchy",
            ),
            "discord:channel:admin": WorkspaceProfile(
                jid="discord:channel:admin",
                name="Admin",
                folder="admin",
                trigger="@Pynchy",
                is_admin=True,
            ),
            "discord:channel:fam-runtime": WorkspaceProfile(
                jid="discord:channel:fam-runtime",
                name="Relationships/fam | fam-check",
                folder="fam-runtime",
                trigger="@Pynchy",
            ),
            "discord:channel:pynchy-runtime": WorkspaceProfile(
                jid="discord:channel:pynchy-runtime",
                name="Admin/pynchy-dev | pynchy-check",
                folder="pynchy-runtime",
                trigger="@Pynchy",
                is_admin=True,
            ),
        }
        tasks = [
            ScheduledTask(
                id="fam-task",
                group_folder="fam",
                chat_jid="discord:channel:relationships",
                prompt="Check fam.",
                schedule_type="cron",
                schedule_value="0 8 * * *",
                session_policy=SessionPolicy.RESET_BEFORE_RUN,
                config_job_name="fam-check",
                derived_thread_name="fam | fam-check",
                bound_chat_jid="discord:channel:fam-runtime",
                bound_group_folder="fam-runtime",
            ),
            ScheduledTask(
                id="pynchy-task",
                group_folder="pynchy-dev",
                chat_jid="discord:channel:admin",
                prompt="Check Pynchy.",
                schedule_type="cron",
                schedule_value="0 9 * * *",
                session_policy=SessionPolicy.RESET_BEFORE_RUN,
                config_job_name="pynchy-check",
                derived_thread_name="pynchy-dev | pynchy-check",
                bound_chat_jid="discord:channel:pynchy-runtime",
                bound_group_folder="pynchy-runtime",
            ),
        ]

        configure_workspace_placement_for(settings)
        _configure_scheduler_runtime(mock_deps, settings)
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
        ):
            for task in tasks:
                assert await run_scheduled_agent(task, mock_deps) is TurnOutcome.COMPLETED

        assert mock_deps.thread_lookups == []
        assert mock_deps.agent_runs[0]["group"].folder == "fam-runtime"
        assert mock_deps.agent_runs[0]["group"].is_admin is False
        assert mock_deps.agent_runs[1]["group"].folder == "pynchy-runtime"
        assert mock_deps.agent_runs[1]["group"].is_admin is True
