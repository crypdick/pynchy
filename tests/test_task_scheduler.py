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
import inspect
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest
from conftest import make_settings

from pynchy.config import SchedulerConfig
from pynchy.config.jobs import JobConfig
from pynchy.config.models import ProfileConfig, WorkspaceConfig
from pynchy.host.orchestrator import task_scheduler as ts_mod
from pynchy.host.orchestrator.concurrency import GroupQueue
from pynchy.host.orchestrator.task_scheduler import run_scheduled_agent, start_scheduler_loop
from pynchy.host.orchestrator.threads import EnsuredThread
from pynchy.host.orchestrator.workspace_config import dynamic_thread_folder
from pynchy.state import (
    begin_in_flight_turn,
    create_task,
    get_in_flight_turn_for_task,
    get_task_by_id,
    get_task_run_logs,
    init_test_database,
    prepare_in_flight_turn_recovery,
)
from pynchy.types import (
    ContainerOutput,
    InFlightTurn,
    InFlightWorkKind,
    ScheduledTask,
    TaskRunLog,
    WorkspaceProfile,
)
from pynchy.utils import ShellResult

TEMPORAL_UNAVAILABLE_MESSAGE = "temporal unavailable"
TEST_ERROR_MESSAGE = "Test error"
AGENT_FAILED_MESSAGE = "Agent failed"


@contextlib.contextmanager
def _patch_settings(*, poll_interval: float = 5.0, groups_dir=None, jobs=None):
    overrides = {
        "scheduler": SchedulerConfig(poll_interval=poll_interval),
        "jobs": jobs or {},
    }
    if groups_dir is not None:
        overrides["groups_dir"] = groups_dir
    s = make_settings(**overrides)
    with patch("pynchy.host.orchestrator.task_scheduler.get_settings", return_value=s):
        yield


class RecordingTemporalRuntime:
    instances = []

    def __init__(self, deps, scheduler_config):
        self.deps = deps
        self.scheduler_config = scheduler_config
        self.reconcile_count = 0
        RecordingTemporalRuntime.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, _tb):
        return None

    async def reconcile_schedules(self):
        self.reconcile_count += 1


@dataclass(frozen=True)
class _ActivityInfo:
    workflow_id: str
    workflow_run_id: str
    attempt: int


@contextlib.contextmanager
def _patch_scheduler_temporal_runtime(runtime_cls=RecordingTemporalRuntime):
    runtime_cls.instances = []
    with patch(
        "pynchy.host.orchestrator.task_scheduler.TemporalSchedulerRuntime",
        new=runtime_cls,
        create=True,
    ):
        yield runtime_cls


async def _run_due_task_via_scheduler(deps, task: ScheduledTask) -> None:
    """Run the public scheduled-agent runner under the caller's patches."""
    if isinstance(ts_mod.get_task_run_logs, Mock):
        await run_scheduled_agent(task, deps)
        return

    with patch(
        "pynchy.host.orchestrator.task_scheduler.get_task_run_logs",
        new_callable=AsyncMock,
        return_value=[],
    ):
        await run_scheduled_agent(task, deps)


async def _run_scheduler_reconcile_once(deps) -> type[RecordingTemporalRuntime]:
    """Drive the public scheduler loop through one Temporal reconciliation poll."""

    def stop_after_poll(delay):
        raise asyncio.CancelledError

    with (
        patch("pynchy.host.orchestrator.task_scheduler.asyncio.sleep", side_effect=stop_after_poll),
        _patch_scheduler_temporal_runtime() as runtime_cls,
    ):
        with contextlib.suppress(asyncio.CancelledError):
            await start_scheduler_loop(deps)
    return runtime_cls


class TestScheduledTaskSnapshotDict:
    """Test ScheduledTask.to_snapshot_dict() serialization.

    This method is used by both app.py and task_scheduler.py to build
    the tasks snapshot written to IPC for containers. Getting the field
    mapping wrong would break container task visibility.
    """

    def test_includes_all_required_fields(self):
        task = ScheduledTask(
            id="task-42",
            group_folder="my-group",
            chat_jid="jid@g.us",
            prompt="Do something",
            schedule_type="cron",
            schedule_value="0 9 * * *",
            context_mode="isolated",
            next_run="2026-02-15T09:00:00+00:00",
            status="active",
        )
        d = task.to_snapshot_dict()
        assert d == {
            "id": "task-42",
            "type": "agent",
            "groupFolder": "my-group",
            "prompt": "Do something",
            "schedule_type": "cron",
            "schedule_value": "0 9 * * *",
            "status": "active",
            "next_run": None,
        }

    def test_next_run_none(self):
        """Once tasks may have no next_run — ensure it serializes as None."""
        task = ScheduledTask(
            id="task-once",
            group_folder="g",
            chat_jid="j@g.us",
            prompt="p",
            schedule_type="once",
            schedule_value="2026-01-01T00:00:00",
            context_mode="isolated",
            next_run=None,
            status="completed",
        )
        d = task.to_snapshot_dict()
        assert d["next_run"] is None
        assert d["status"] == "completed"

    def test_uses_camel_case_group_folder(self):
        """Container expects 'groupFolder' (camelCase), not 'group_folder'."""
        task = ScheduledTask(
            id="t",
            group_folder="test-folder",
            chat_jid="j@g.us",
            prompt="p",
            schedule_type="interval",
            schedule_value="60000",
            context_mode="group",
        )
        d = task.to_snapshot_dict()
        assert "groupFolder" in d
        assert "group_folder" not in d
        assert d["groupFolder"] == "test-folder"

    def test_excludes_internal_fields(self):
        """Fields like chat_jid, context_mode, repo_access are internal
        and should not leak into the snapshot dict."""
        task = ScheduledTask(
            id="t",
            group_folder="g",
            chat_jid="secret@g.us",
            prompt="p",
            schedule_type="cron",
            schedule_value="* * * * *",
            context_mode="group",
            repo_access="owner/pynchy",
            last_run="2026-01-01",
            last_result="ok",
            created_at="2026-01-01",
        )
        d = task.to_snapshot_dict()
        assert "chat_jid" not in d
        assert "context_mode" not in d
        assert "repo_access" not in d
        assert "last_run" not in d
        assert "last_result" not in d
        assert "created_at" not in d


class MockSchedulerDeps:
    """Mock implementation of SchedulerDependencies protocol."""

    def __init__(self):
        self.groups: dict[str, WorkspaceProfile] = {}
        self.queue = GroupQueue()
        self.messages: list = []
        self.host_messages: list = []
        self.system_notices: list = []
        self.agent_runs: list = []
        self.streamed_outputs: list = []
        self.thread_creations: list[tuple[str, str]] = []
        self.thread_participants: list[tuple[str, ...]] = []
        self.existing_threads: dict[str, str] = {}
        self.thread_lookups: list[tuple[str, str]] = []
        self.reused_thread_participants: list[tuple[str, tuple[str, ...]]] = []
        self.thread_creation_supported = True
        # Configurable return value for run_agent
        self._run_agent_result: str = "success"
        # Configurable side effect for run_agent (to call on_output)
        self._run_agent_side_effect = None

    @property
    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return self.groups

    async def register_workspace(self, profile: WorkspaceProfile) -> None:
        self.groups[profile.jid] = profile

    async def supports_thread_creation(self, parent_jid: str) -> bool:
        del parent_jid
        return self.thread_creation_supported

    async def broadcast_to_channels(self, jid: str, event) -> None:
        self.messages.append((jid, event))

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None:
        self.host_messages.append((chat_jid, text))

    async def broadcast_system_notice(self, chat_jid: str, text: str) -> None:
        self.system_notices.append((chat_jid, text))

    async def create_thread(
        self,
        parent_jid: str,
        name: str,
        *,
        participant_ids: tuple[str, ...] = (),
    ) -> str:
        self.thread_creations.append((parent_jid, name))
        self.thread_participants.append(participant_ids)
        child_jid = f"discord:channel:scheduled-{len(self.thread_creations)}"
        self.existing_threads[name] = child_jid
        return child_jid

    async def find_thread(self, parent_jid: str, name: str) -> str | None:
        self.thread_lookups.append((parent_jid, name))
        return self.existing_threads.get(name)

    async def add_thread_participants(
        self,
        child_jid: str,
        participant_ids: tuple[str, ...],
    ) -> None:
        self.reused_thread_participants.append((child_jid, participant_ids))

    async def ensure_thread(
        self,
        parent_jid: str,
        name: str,
        *,
        participant_ids: tuple[str, ...] = (),
    ) -> EnsuredThread:
        child_jid = await self.find_thread(parent_jid, name)
        if child_jid is not None:
            await self.add_thread_participants(child_jid, participant_ids)
            return EnsuredThread(jid=child_jid, created=False)
        return EnsuredThread(
            jid=await self.create_thread(
                parent_jid,
                name,
                participant_ids=participant_ids,
            ),
            created=True,
        )

    async def run_agent(
        self,
        group,
        chat_jid,
        messages,
        on_output=None,
        extra_system_notices=None,
        *,
        is_scheduled_task=False,
        repo_access_override=None,
        input_source="user",
        turn_id=None,
    ) -> str:
        self.agent_runs.append(
            {
                "group": group,
                "chat_jid": chat_jid,
                "messages": messages,
                "on_output": on_output,
                "is_scheduled_task": is_scheduled_task,
                "repo_access_override": repo_access_override,
                "input_source": input_source,
                "turn_id": turn_id,
            }
        )
        if self._run_agent_side_effect:
            result = self._run_agent_side_effect(
                group,
                chat_jid,
                messages,
                on_output,
                is_scheduled_task=is_scheduled_task,
                repo_access_override=repo_access_override,
                input_source=input_source,
                turn_id=turn_id,
            )
            if inspect.isawaitable(result):
                return await result
            return result
        return self._run_agent_result

    async def handle_streamed_output(self, chat_jid, group, result, *, turn_id=None) -> bool:
        self.streamed_outputs.append((chat_jid, group, result, turn_id))
        return bool(result.result)


@pytest.fixture
def mock_deps():
    """Create mock scheduler dependencies."""
    return MockSchedulerDeps()


@pytest.fixture
def sample_task():
    """Create a sample scheduled task."""
    return ScheduledTask(
        id="task-1",
        group_folder="test-group",
        chat_jid="test@g.us",
        prompt="Test task",
        schedule_type="cron",
        schedule_value="0 9 * * *",
        context_mode="isolated",
        next_run=datetime.now(UTC).isoformat(),
        status="active",
    )


@pytest.fixture
def sample_group():
    """Create a sample registered group."""
    return WorkspaceProfile(
        jid="test@g.us",
        name="Test Group",
        folder="test-group",
        trigger="@bot",
        added_at=datetime.now(UTC).isoformat(),
    )


class TestStartSchedulerLoop:
    """Test the scheduler loop initialization and duplicate prevention."""

    def test_scheduler_config_defaults_to_temporal_connection(self):
        """Scheduler config exposes the Temporal connection without a local backend switch."""
        cfg = SchedulerConfig()

        assert not hasattr(cfg, "backend")
        assert cfg.temporal_address == "localhost:7233"
        assert cfg.temporal_namespace == "default"
        assert cfg.temporal_task_queue == "pynchy-scheduler"

    @pytest.mark.asyncio
    async def test_scheduler_reconciles_temporal_schedules_without_local_due_polling(
        self, mock_deps
    ):
        """The scheduler loop reconciles Temporal schedules instead of running due work."""
        enqueued = []
        mock_deps.queue.enqueue_task = lambda group_jid, task_id, fn: enqueued.append(task_id)

        scheduler = SchedulerConfig(
            poll_interval=0.01,
            temporal_address="localhost:7233",
            temporal_namespace="default",
            temporal_task_queue="pynchy-test",
        )

        with (
            patch(
                "pynchy.host.orchestrator.task_scheduler.asyncio.create_subprocess_shell",
                new_callable=AsyncMock,
            ) as mock_spawn,
            _patch_scheduler_temporal_runtime() as runtime_cls,
            patch(
                "pynchy.host.orchestrator.task_scheduler.asyncio.sleep",
                side_effect=asyncio.CancelledError,
            ),
        ):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.get_settings",
                return_value=make_settings(scheduler=scheduler),
            ):
                with contextlib.suppress(asyncio.CancelledError):
                    await start_scheduler_loop(mock_deps)

        assert enqueued == []
        assert len(runtime_cls.instances) == 1
        assert runtime_cls.instances[0].reconcile_count == 1
        mock_spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_prevents_duplicate_scheduler_start(self, mock_deps):
        """Should prevent starting multiple scheduler loops."""
        with _patch_scheduler_temporal_runtime() as runtime_cls:
            # Start first scheduler
            task1 = asyncio.create_task(start_scheduler_loop(mock_deps))
            await asyncio.sleep(0.01)  # Let it start

            # Try to start second scheduler
            task2 = asyncio.create_task(start_scheduler_loop(mock_deps))
            await asyncio.sleep(0.01)

            # Cancel both
            task1.cancel()
            task2.cancel()

            with contextlib.suppress(asyncio.CancelledError):
                await task1

            with contextlib.suppress(asyncio.CancelledError):
                await task2

        assert len(runtime_cls.instances) == 1

    @pytest.mark.asyncio
    async def test_scheduler_start_can_retry_after_temporal_start_failure(self, mock_deps):
        """A failed Temporal startup should not poison later scheduler starts."""

        class FailingTemporalRuntime:
            instances = []

            def __init__(self, deps, scheduler_config):
                FailingTemporalRuntime.instances.append(self)

            async def __aenter__(self):
                raise RuntimeError(TEMPORAL_UNAVAILABLE_MESSAGE)

            async def __aexit__(self, exc_type, exc, _tb):
                pass

        def stop_on_first_poll(delay):
            raise asyncio.CancelledError

        with _patch_scheduler_temporal_runtime(FailingTemporalRuntime):
            with pytest.raises(RuntimeError, match="temporal unavailable"):
                await start_scheduler_loop(mock_deps)

        with (
            _patch_scheduler_temporal_runtime() as runtime_cls,
            patch(
                "pynchy.host.orchestrator.task_scheduler.asyncio.sleep",
                side_effect=stop_on_first_poll,
            ),
            _patch_settings(poll_interval=0.01),
        ):
            with contextlib.suppress(asyncio.CancelledError):
                await start_scheduler_loop(mock_deps)

        assert len(FailingTemporalRuntime.instances) == 1
        assert len(runtime_cls.instances) == 1

    @pytest.mark.asyncio
    async def test_scheduler_loop_reconciles_temporal_schedules_repeatedly(self, mock_deps):
        """Should continuously reconcile Temporal schedules."""
        sleep_count = 0

        def mock_sleep(delay):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 2:
                raise asyncio.CancelledError

        with _patch_scheduler_temporal_runtime() as runtime_cls:
            with patch(
                "pynchy.host.orchestrator.task_scheduler.asyncio.sleep", side_effect=mock_sleep
            ):
                with _patch_settings(poll_interval=0.01):
                    with contextlib.suppress(asyncio.CancelledError):
                        await start_scheduler_loop(mock_deps)

        assert runtime_cls.instances[0].reconcile_count >= 2

    @pytest.mark.asyncio
    async def test_scheduler_loop_handles_exceptions_gracefully(self, mock_deps):
        """Should catch and log exceptions without crashing."""
        error_count = 0

        def mock_reconcile():
            nonlocal error_count
            error_count += 1
            if error_count == 1:
                raise ValueError(TEST_ERROR_MESSAGE)

        def mock_sleep(delay):
            if error_count >= 2:
                raise asyncio.CancelledError

        with _patch_scheduler_temporal_runtime() as runtime_cls:
            with patch.object(runtime_cls, "reconcile_schedules", side_effect=mock_reconcile):
                with patch(
                    "pynchy.host.orchestrator.task_scheduler.asyncio.sleep", side_effect=mock_sleep
                ):
                    with _patch_settings(poll_interval=0.01):
                        with contextlib.suppress(asyncio.CancelledError):
                            await start_scheduler_loop(mock_deps)

                    # Should have continued after the error
                    assert error_count >= 2

    @pytest.mark.asyncio
    async def test_scheduler_does_not_enqueue_due_tasks_locally(self, mock_deps):
        """Due work is Temporal schedule state, not local GroupQueue work."""
        enqueued = []

        def track_enqueue(group_jid, task_id, fn):
            enqueued.append((group_jid, task_id))

        mock_deps.queue.enqueue_task = track_enqueue

        with _patch_scheduler_temporal_runtime() as runtime_cls:
            with patch(
                "pynchy.host.orchestrator.task_scheduler.asyncio.sleep",
                side_effect=asyncio.CancelledError,
            ):
                with _patch_settings(poll_interval=0.01):
                    with contextlib.suppress(asyncio.CancelledError):
                        await start_scheduler_loop(mock_deps)

        assert enqueued == []
        assert len(runtime_cls.instances) == 1


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
        assert "Group not found" in logged_runs[0].error

    @pytest.mark.asyncio
    async def test_calls_run_agent_with_correct_flags(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Should call run_agent with is_scheduled_task=True and input_source='scheduled_task'."""
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

        assert len(mock_deps.agent_runs) == 1
        run = mock_deps.agent_runs[0]
        assert run["is_scheduled_task"] is True
        assert run["input_source"] == "scheduled_task"
        assert isinstance(run["turn_id"], str)
        assert run["turn_id"].startswith("turn_")
        assert run["chat_jid"] == "test@g.us"
        # Verify prompt was passed as a user message
        assert len(run["messages"]) == 1
        assert run["messages"][0]["content"] == "Test task"
        assert run["messages"][0]["sender"] == "scheduled_task"

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
        assert "continue the unfinished job" in resumed_run["messages"][0]["content"]
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
            assert await run_scheduled_agent(sample_task, mock_deps) is True
            assert await run_scheduled_agent(sample_task, mock_deps) is True

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
            assert await run_scheduled_agent(sample_task, mock_deps) is False
            checkpoint = await get_in_flight_turn_for_task(sample_task.id)
            assert checkpoint is not None

            assert await run_scheduled_agent(sample_task, mock_deps) is False
            await prepare_in_flight_turn_recovery("deploy-sha")
            mock_deps._run_agent_result = "success"
            assert await run_scheduled_agent(sample_task, mock_deps) is True

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

    @pytest.mark.asyncio
    async def test_uses_numbered_thread_when_human_turn_reserves_base_channel(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """A live human turn keeps the configured channel out of scheduled work."""
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

        assert mock_deps.thread_creations == [("test@g.us", "test-group-1")]
        assert mock_deps.thread_participants == [("123456",)]
        scheduled_run = mock_deps.agent_runs[0]
        assert scheduled_run["chat_jid"] == "discord:channel:scheduled-1"
        assert scheduled_run["group"].folder == "test-group__thread_discord-channel-scheduled-1"
        assert mock_deps.messages[0][0] == "discord:channel:scheduled-1"

    @pytest.mark.asyncio
    async def test_uses_numbered_thread_when_human_session_is_live(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """A warm human session remains available while scheduled work runs elsewhere."""
        mock_deps.groups["test-jid"] = sample_group

        with (
            patch(
                "pynchy.host.orchestrator.scheduled_targeting.get_session",
                return_value=Mock(is_alive=True),
            ),
            patch("pynchy.host.orchestrator.task_scheduler.log_task_run", new_callable=AsyncMock),
            patch(
                "pynchy.host.orchestrator.task_scheduler.record_task_completion",
                new_callable=AsyncMock,
            ),
            patch("pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock),
            _patch_settings(groups_dir=tmp_path, poll_interval=0.01),
        ):
            await _run_due_task_via_scheduler(mock_deps, sample_task)

        assert mock_deps.thread_creations == [("test@g.us", "test-group-1")]
        assert mock_deps.agent_runs[0]["chat_jid"] == "discord:channel:scheduled-1"

    @pytest.mark.asyncio
    async def test_reuses_idle_existing_numbered_thread(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Existing matching child threads are reusable scheduled-task slots."""
        mock_deps.groups["test-jid"] = sample_group
        existing_jid = "discord:channel:existing-1"
        mock_deps.existing_threads["test-group-1"] = existing_jid
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

        assert mock_deps.thread_lookups == [("test@g.us", "test-group-1")]
        assert mock_deps.thread_creations == []
        assert mock_deps.reused_thread_participants == [(existing_jid, ("123456",))]
        assert mock_deps.agent_runs[0]["chat_jid"] == existing_jid

    @pytest.mark.asyncio
    async def test_skips_reserved_existing_numbered_thread(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """An active numbered child thread makes the allocator continue to the next slot."""
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

        assert mock_deps.thread_lookups == [
            ("test@g.us", "test-group-1"),
            ("test@g.us", "test-group-2"),
        ]
        assert mock_deps.thread_creations == [("test@g.us", "test-group-2")]
        assert mock_deps.agent_runs[0]["chat_jid"] == "discord:channel:scheduled-1"

    @pytest.mark.asyncio
    async def test_uses_numbered_thread_when_another_scheduled_task_reserves_base_channel(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Overlapping scheduled runs keep their agent sessions and output separate."""
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
            context_mode=sample_task.context_mode,
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
            first_run = asyncio.create_task(run_scheduled_agent(sample_task, mock_deps))
            await first_started.wait()
            await run_scheduled_agent(second_task, mock_deps)
            release_first.set()
            await first_run

        assert mock_deps.thread_creations == [("test@g.us", "test-group-1")]
        assert [run["chat_jid"] for run in mock_deps.agent_runs] == [
            "test@g.us",
            "discord:channel:scheduled-1",
        ]

    @pytest.mark.asyncio
    async def test_defers_when_durable_owner_reserves_target_without_child_threads(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """A failed sibling task keeps sole ownership of a non-nestable target."""
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
                scheduled_base_chat_jid=sample_task.chat_jid,
                scheduled_thread_slot=0,
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
            with pytest.raises(
                RuntimeError,
                match="target is reserved and cannot host child threads",
            ):
                await run_scheduled_agent(sample_task, mock_deps)

        assert mock_deps.thread_lookups == []
        assert mock_deps.thread_creations == []
        assert mock_deps.agent_runs == []
        assert await get_in_flight_turn_for_task("prior-task") is not None
        assert await get_in_flight_turn_for_task(sample_task.id) is None
        log_task_run.assert_not_awaited()
        record_task_completion.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_config_job_ensures_its_derived_thread_on_each_run(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Every config job run uses its one derived sibling thread."""
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

        assert mock_deps.thread_lookups == [
            ("test@g.us", "test-group | fam_daily_checkin"),
            ("test@g.us", "test-group | fam_daily_checkin"),
        ]
        assert mock_deps.thread_creations == [("test@g.us", "test-group | fam_daily_checkin")]
        assert [run["chat_jid"] for run in mock_deps.agent_runs] == [
            "discord:channel:scheduled-1",
            "discord:channel:scheduled-1",
        ]
        expected_folder = dynamic_thread_folder(
            sample_task.group_folder,
            "discord:channel:scheduled-1",
        )
        assert [run["group"].folder for run in mock_deps.agent_runs] == [
            expected_folder,
            expected_folder,
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
        }
        tasks = [
            ScheduledTask(
                id="fam-task",
                group_folder="fam",
                chat_jid="discord:channel:relationships",
                prompt="Check fam.",
                schedule_type="cron",
                schedule_value="0 8 * * *",
                context_mode="isolated",
                config_job_name="fam-check",
                derived_thread_name="fam | fam-check",
            ),
            ScheduledTask(
                id="pynchy-task",
                group_folder="pynchy-dev",
                chat_jid="discord:channel:admin",
                prompt="Check Pynchy.",
                schedule_type="cron",
                schedule_value="0 9 * * *",
                context_mode="isolated",
                config_job_name="pynchy-check",
                derived_thread_name="pynchy-dev | pynchy-check",
            ),
        ]

        with (
            patch("pynchy.host.orchestrator.task_scheduler.get_settings", return_value=settings),
            patch(
                "pynchy.host.orchestrator.config_job_execution.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.orchestrator.workspace_placement.get_settings",
                return_value=settings,
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
            for task in tasks:
                assert await run_scheduled_agent(task, mock_deps) is True

        assert mock_deps.thread_lookups == [
            ("discord:channel:relationships", "fam | fam-check"),
            ("discord:channel:admin", "pynchy-dev | pynchy-check"),
        ]
        assert mock_deps.agent_runs[0]["group"].folder.startswith("fam__thread_")
        assert mock_deps.agent_runs[0]["group"].is_admin is False
        assert mock_deps.agent_runs[1]["group"].folder.startswith("pynchy-dev__thread_")
        assert mock_deps.agent_runs[1]["group"].is_admin is True
        assert mock_deps.groups["discord:channel:scheduled-1"].folder.startswith("fam__thread_")
        assert mock_deps.groups["discord:channel:scheduled-2"].folder.startswith(
            "pynchy-dev__thread_"
        )

    @pytest.mark.asyncio
    async def test_deterministic_scoped_job_registers_owner_thread_without_agent(
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
            )
        }
        task = ScheduledTask(
            id="watchdog-task",
            group_folder="cron",
            chat_jid="discord:channel:ops",
            prompt="",
            schedule_type="cron",
            schedule_value="0 23 * * *",
            context_mode="isolated",
            config_job_name="watchdog",
            derived_thread_name="cron | watchdog",
        )

        with (
            patch("pynchy.host.orchestrator.task_scheduler.get_settings", return_value=settings),
            patch(
                "pynchy.host.orchestrator.config_job_execution.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.orchestrator.workspace_placement.get_settings",
                return_value=settings,
            ),
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
            assert await run_scheduled_agent(task, mock_deps) is True

        assert mock_deps.agent_runs == []
        assert mock_deps.thread_creations == [("discord:channel:ops", "cron | watchdog")]
        assert mock_deps.host_messages == [("discord:channel:scheduled-1", "watchdog ok")]
        assert mock_deps.groups["discord:channel:scheduled-1"].folder.startswith("cron__thread_")

    @pytest.mark.asyncio
    async def test_pre_run_false_gate_skips_before_thread_creation(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        sample_task.config_job_name = "gated-job"
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

        with (
            patch("pynchy.host.orchestrator.task_scheduler.get_settings", return_value=settings),
            patch(
                "pynchy.host.orchestrator.config_job_execution.get_settings",
                return_value=settings,
            ),
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
            assert await run_scheduled_agent(sample_task, mock_deps) is True

        assert mock_deps.thread_lookups == []
        assert mock_deps.thread_creations == []
        assert mock_deps.agent_runs == []

    @pytest.mark.asyncio
    async def test_config_job_recreates_a_missing_derived_thread(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """A deleted destination gets recreated instead of leaving a stale task target."""
        mock_deps.groups["test-jid"] = sample_group
        sample_task.config_job_name = "fam_daily_checkin"
        name = "test-group | fam_daily_checkin"

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
            del mock_deps.existing_threads[name]
            await _run_due_task_via_scheduler(mock_deps, sample_task)

        assert mock_deps.thread_creations == [
            ("test@g.us", name),
            ("test@g.us", name),
        ]
        assert [run["chat_jid"] for run in mock_deps.agent_runs] == [
            "discord:channel:scheduled-1",
            "discord:channel:scheduled-2",
        ]

    @pytest.mark.asyncio
    async def test_config_job_rename_selects_a_new_human_readable_thread(
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

        assert mock_deps.thread_creations == [
            ("test@g.us", "test-group | fam_daily_checkin"),
            ("test@g.us", "test-group | family_daily_checkin"),
        ]

    @pytest.mark.asyncio
    async def test_config_jobs_in_one_workspace_run_concurrently_in_separate_threads(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Distinct job names do not serialize through their shared root."""
        mock_deps.groups["test-jid"] = sample_group
        sample_task.config_job_name = "fam_daily_checkin"
        second_task = ScheduledTask(
            id="task-2",
            group_folder=sample_task.group_folder,
            chat_jid=sample_task.chat_jid,
            prompt="Tend the garden.",
            schedule_type=sample_task.schedule_type,
            schedule_value=sample_task.schedule_value,
            context_mode=sample_task.context_mode,
            config_job_name="fam_gardener",
        )
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def mock_run(_group, chat_jid, _messages, _on_output, **_kwargs):
            if chat_jid == "discord:channel:scheduled-1":
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

        assert mock_deps.thread_creations == [
            ("test@g.us", "test-group | fam_daily_checkin"),
            ("test@g.us", "test-group | fam_gardener"),
        ]
        assert [run["chat_jid"] for run in mock_deps.agent_runs] == [
            "discord:channel:scheduled-1",
            "discord:channel:scheduled-2",
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

        assert mock_deps.thread_creations == [("test@g.us", "test-group | fam_daily_checkin")]
        assert [run["chat_jid"] for run in mock_deps.agent_runs] == [
            "discord:channel:scheduled-1",
            "discord:channel:scheduled-1",
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
            assert await run_scheduled_agent(sample_task, mock_deps) is False
            reloaded_task = await get_task_by_id(sample_task.id)
            assert reloaded_task is not None
            assert reloaded_task is not sample_task
            assert reloaded_task.config_job_name == "fam_daily_checkin"
            lookups_before_retry = len(mock_deps.thread_lookups)
            creations_before_retry = len(mock_deps.thread_creations)
            mock_deps._run_agent_result = "success"
            assert await run_scheduled_agent(reloaded_task, mock_deps) is True

        assert mock_deps.thread_creations == [("test@g.us", "test-group | fam_daily_checkin")]
        assert len(mock_deps.thread_lookups) == lookups_before_retry
        assert len(mock_deps.thread_creations) == creations_before_retry
        assert [run["chat_jid"] for run in mock_deps.agent_runs] == [
            "discord:channel:scheduled-1",
            "discord:channel:scheduled-1",
        ]

    @pytest.mark.asyncio
    async def test_resumes_scheduled_task_in_its_original_numbered_thread(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Deploy recovery never creates a second child thread for the same task."""
        mock_deps.groups["test-jid"] = sample_group
        child_jid = "discord:channel:scheduled-1"
        child_folder = "test-group__thread_discord-channel-scheduled-1"
        await begin_in_flight_turn(
            InFlightTurn(
                turn_id="scheduled-turn",
                chat_jid=child_jid,
                group_folder=child_folder,
                work_kind=InFlightWorkKind.SCHEDULED,
                input_messages=[{"content": sample_task.prompt}],
                input_start_cursor="",
                input_end_cursor="",
                started_at=datetime.now(UTC).isoformat(),
                task_id=sample_task.id,
                scheduled_base_chat_jid=sample_task.chat_jid,
                scheduled_thread_slot=1,
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
        assert mock_deps.agent_runs[0]["chat_jid"] == child_jid
        assert mock_deps.agent_runs[0]["group"].folder == child_folder

    @pytest.mark.asyncio
    async def test_retry_reuses_original_numbered_thread_after_agent_error(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """A retry keeps the first child target rather than creating another one."""
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
            assert await run_scheduled_agent(sample_task, mock_deps) is False
            checkpoint = await get_in_flight_turn_for_task(sample_task.id)
            assert checkpoint is not None

            mock_deps._run_agent_result = "success"
            assert await run_scheduled_agent(sample_task, mock_deps) is True

        assert mock_deps.thread_creations == [("test@g.us", "test-group-1")]
        assert [run["chat_jid"] for run in mock_deps.agent_runs] == [
            "discord:channel:scheduled-1",
            "discord:channel:scheduled-1",
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

        assert completed is False
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
