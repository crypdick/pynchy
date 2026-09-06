"""Tests for the Temporal scheduler control-plane integration."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from conftest import make_settings
from temporalio.client import ScheduleOverlapPolicy

import pynchy.host.orchestrator.temporal.deploy as temporal_deploy
import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler
import pynchy.host.orchestrator.temporal.schedules as temporal_schedules
import pynchy.host.orchestrator.temporal.workflows as temporal_workflows
from pynchy.config.api import (
    SchedulerConfig,
)
from pynchy.deployments import (
    DeployChangeKind,
    DeployClaimStatus,
    DeployRevision,
)
from pynchy.learning_packets import packet_to_payload
from pynchy.linear_plan_types import LinearPlanReviewAdmission
from pynchy.state import (
    init_test_database,
    initialize_deployment_state,
)
from tests.temporal_scheduler_support import (
    DeduplicatingFakeScheduleClient,
    FakeScheduleClient,
    NullSchedulerDeps,
    _scheduler_runtime,
    _WorkflowListEntry,
)

pytest_plugins = ("tests.temporal_scheduler_support",)

TEMPORAL_UNAVAILABLE_MESSAGE = "temporal unavailable"
PAUSED_TASK_RUN_MESSAGE = "paused tasks must not run"


class TestTemporalSchedulerRuntime:
    @staticmethod
    def _capturing_worker(captured: dict[str, object]):
        @asynccontextmanager
        async def fake_worker(*args, workflows, activities, **kwargs):
            captured["workflows"] = workflows
            captured["activities"] = activities
            yield object()

        return fake_worker

    @staticmethod
    def _assert_registered_temporal_workflows(
        captured: dict[str, object], temporal_scheduler
    ) -> None:
        assert {
            temporal_workflows.InteractiveMessageWorkflow,
            temporal_workflows.InterruptedTurnWorkflow,
            temporal_workflows.LearningReviewWorkflow,
            temporal_workflows.DeployWorkflow,
            temporal_workflows.HostGitSyncWorkflow,
            temporal_workflows.ExternalGitSyncWorkflow,
            temporal_workflows.ChannelReconciliationWorkflow,
            temporal_workflows.LinearWorkItemReconciliationWorkflow,
            temporal_workflows.LinearPlanReviewWorkflow,
        }.issubset(set(captured["workflows"]))
        assert {
            temporal_scheduler.run_interactive_message_turn,
            temporal_scheduler.run_interactive_runtime_turn,
            temporal_scheduler.run_interrupted_agent_turn,
            temporal_scheduler.clear_terminal_scheduled_turn,
            temporal_scheduler.run_learning_review,
            temporal_scheduler.run_deploy,
            temporal_scheduler.run_host_git_sync,
            temporal_scheduler.run_external_git_sync,
            temporal_scheduler.run_channel_reconciliation,
            temporal_scheduler.run_linear_work_item_reconciliation,
            temporal_scheduler.run_linear_plan_review_admission,
        }.issubset(set(captured["activities"]))

    @staticmethod
    def _assert_finalize_deploy_kwargs(finalize_deploy: AsyncMock, deps: NullSchedulerDeps) -> None:
        finalize_deploy.assert_awaited_once()
        assert finalize_deploy.await_args.kwargs == {
            "broadcast_host_message": deps.broadcast_host_message,
            "chat_jid": "slack:C123",
            "commit_sha": "new-sha",
            "config_hash": "config-hash",
            "previous_sha": "old-sha",
            "change_kind": DeployChangeKind.CODE,
            "data_dir": deps.agent_execution_runtime.data_dir,
            "resume_prompt": "Deploy complete. Verifying service health.",
            "sigterm_delay": 0.25,
        }

    @staticmethod
    def _assert_scheduler_status(
        temporal_scheduler,
        *,
        workflow_id: str,
        task_id: str,
        result: str,
    ) -> None:
        status = temporal_scheduler.get_temporal_scheduler_status()
        assert status["last_workflow_id"] == workflow_id
        assert status["last_task_id"] == task_id
        assert status["last_result"] == result

    def test_scheduler_status_defaults_to_stopped(self):

        temporal_scheduler.reset_temporal_scheduler_status()

        status = temporal_scheduler.get_temporal_scheduler_status()

        assert status == {
            "worker_running": False,
            "last_workflow_id": None,
            "last_task_id": None,
            "last_result": None,
            "last_started_at": None,
            "last_completed_at": None,
            "last_error": None,
            "tracked_results": {},
        }

    def test_scheduler_workflow_runner_passes_through_workflow_module(self):

        runner = temporal_scheduler.scheduler_workflow_runner()

        assert (
            "pynchy.host.orchestrator.temporal.workflows" in runner.restrictions.passthrough_modules
        )
        assert "pynchy.turn_outcomes" in runner.restrictions.passthrough_modules

    def test_agent_task_workflow_id_is_stable_and_temporal_safe(self, temporal_task):
        workflow_id = temporal_scheduler.agent_task_workflow_id(temporal_task)

        assert workflow_id == "pynchy-agent-task-task-with-spaces-0-9"

    def test_resumed_once_task_workflow_id_has_durable_occurrence(self, temporal_task):
        temporal_task.occurrence_generation = 2
        temporal_task.occurrence_due_at = "2026-07-26T06:00:00+00:00"

        workflow_id = temporal_scheduler.agent_task_workflow_id(temporal_task)

        assert "-603cf887c3ffad1a-" in workflow_id
        assert workflow_id.endswith("-2026-07-26T06-00-00-00-00-resume-2")

        temporal_task.schedule_value = "2027-01-01T00:00:00+00:00"
        assert temporal_scheduler.agent_task_workflow_id(temporal_task) == workflow_id

    def test_resumed_once_task_workflow_id_distinguishes_normalization_collisions(
        self, temporal_task
    ):
        temporal_task.schedule_type = "once"
        temporal_task.id = "task/a"
        temporal_task.occurrence_generation = 1
        colliding_task = replace(temporal_task, id="task?a")

        assert temporal_schedules.safe_workflow_fragment(
            temporal_task.id
        ) == temporal_schedules.safe_workflow_fragment(colliding_task.id)
        assert temporal_scheduler.agent_task_workflow_id(
            temporal_task
        ) != temporal_scheduler.agent_task_workflow_id(colliding_task)

    def test_agent_task_schedule_id_is_stable_and_temporal_safe(self, temporal_task):
        schedule_id = temporal_scheduler.agent_task_schedule_id(temporal_task)

        assert schedule_id == "pynchy-agent-schedule-task-with-spaces"

    def test_learning_review_workflow_id_is_stable_and_temporal_safe(self, learning_packet):
        workflow_id = temporal_scheduler.learning_review_workflow_id(learning_packet)

        assert workflow_id == "pynchy-learning-review-learning-job-one"

    def test_interactive_message_workflow_id_is_stable_and_temporal_safe(self):
        workflow_id = temporal_scheduler.interactive_message_workflow_id("slack:C123/with spaces")

        assert workflow_id == "pynchy-interactive-turn-slack-C123-with-spaces"

    def test_interrupted_turn_workflow_id_is_stable_and_temporal_safe(self):
        workflow_id = temporal_scheduler.interrupted_turn_workflow_id("turn:123/with spaces")

        assert workflow_id == "pynchy-interrupted-turn-turn-123-with-spaces"

    def test_deploy_workflow_id_is_stable_and_temporal_safe(self):
        workflow_id = temporal_scheduler.deploy_workflow_id(
            DeployRevision("abc1234/with spaces", "config/hash")
        )

        assert workflow_id == "pynchy-deploy-abc1234-with-spaces-config-hash"

    @pytest.mark.asyncio
    async def test_start_scheduled_agent_task_uses_configured_task_queue(self, temporal_task):
        class FakeClient:
            def __init__(self):
                self.calls = []

            async def start_workflow(
                self,
                workflow,
                *posargs,
                workflow_id=None,
                task_queue,
                id_reuse_policy,
                args=(),
                **kwargs,
            ):
                assert len(posargs) <= 1
                workflow_args = tuple(args)
                assert not (posargs and workflow_args)
                resolved_workflow_id = kwargs.get("id", workflow_id)
                assert resolved_workflow_id is not None
                self.calls.append(
                    {
                        "workflow": workflow,
                        "args": posargs or workflow_args,
                        "id": resolved_workflow_id,
                        "task_queue": task_queue,
                        "id_reuse_policy": id_reuse_policy,
                    }
                )

        scheduler = SchedulerConfig(
            temporal_address="localhost:7233",
            temporal_namespace="default",
            temporal_task_queue="pynchy-test",
        )
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(scheduler)
        )
        runtime.client = FakeClient()

        await runtime.start_scheduled_agent_task(temporal_task)

        assert len(runtime.client.calls) == 1
        call = runtime.client.calls[0]
        assert call["args"] == (temporal_task.id,)
        assert call["id"] == "pynchy-agent-task-task-with-spaces-0-9"
        assert call["task_queue"] == "pynchy-test"
        assert call["id_reuse_policy"].name == "REJECT_DUPLICATE"

    @pytest.mark.asyncio
    async def test_start_scheduled_agent_task_updates_status(self, temporal_task):

        class FakeClient:
            async def start_workflow(
                self,
                workflow,
                *posargs,
                workflow_id=None,
                task_queue,
                id_reuse_policy,
                args=(),
                **kwargs,
            ):
                assert len(posargs) <= 1
                assert not (posargs and args)
                assert kwargs.get("id", workflow_id) is not None

        temporal_scheduler.reset_temporal_scheduler_status()
        scheduler = SchedulerConfig(temporal_task_queue="pynchy-test")
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(scheduler)
        )
        runtime.client = FakeClient()

        await runtime.start_scheduled_agent_task(temporal_task)

        status = temporal_scheduler.get_temporal_scheduler_status()
        assert status["last_workflow_id"] == "pynchy-agent-task-task-with-spaces-0-9"
        assert status["last_task_id"] == temporal_task.id
        assert status["last_result"] == "started"
        assert status["last_started_at"] is not None
        assert status["last_completed_at"] is None

    @pytest.mark.asyncio
    async def test_start_learning_review_starts_temporal_workflow(self, learning_packet):
        client = FakeScheduleClient()
        scheduler = SchedulerConfig(temporal_task_queue="pynchy-test")
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(scheduler)
        )
        runtime.client = client

        await runtime.start_learning_review(learning_packet)

        assert len(client.started_workflows) == 1
        workflow, args, kwargs = client.started_workflows[0]
        assert workflow == temporal_workflows.LearningReviewWorkflow.run
        assert args == (packet_to_payload(learning_packet), 3)
        assert kwargs["id"] == "pynchy-learning-review-learning-job-one"
        assert kwargs["task_queue"] == "pynchy-test"
        assert kwargs["id_reuse_policy"].name == "REJECT_DUPLICATE"

    @pytest.mark.asyncio
    async def test_start_linear_plan_review_deduplicates_exact_issue_revision(self):
        client = FakeScheduleClient()
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(),
            scheduler_config=_scheduler_runtime(SchedulerConfig(temporal_task_queue="pynchy-test")),
        )
        runtime.client = client
        admission = LinearPlanReviewAdmission(
            workspace="project",
            issue_id="issue-1",
            identifier="SYN-1",
            updated_at="2026-07-28T12:00:00Z",
            public_source=True,
        )

        await runtime.start_linear_plan_review(admission)

        workflow, args, kwargs = client.started_workflows[0]
        assert workflow == temporal_workflows.LinearPlanReviewWorkflow.run
        assert args == (admission.to_payload(),)
        assert kwargs["id"] == temporal_scheduler.linear_plan_review_workflow_id(admission)
        assert kwargs["task_queue"] == "pynchy-test"
        assert kwargs["id_reuse_policy"].name == "ALLOW_DUPLICATE_FAILED_ONLY"

    @pytest.mark.asyncio
    async def test_start_interactive_message_turn_starts_temporal_workflow(self):
        client = FakeScheduleClient()
        scheduler = SchedulerConfig(temporal_task_queue="pynchy-test")
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(scheduler)
        )
        runtime.client = client

        await runtime.start_interactive_message_turn("slack:C123")

        assert len(client.started_workflows) == 1
        workflow, args, kwargs = client.started_workflows[0]
        assert workflow == temporal_workflows.InteractiveMessageWorkflow.run
        assert args == ("slack:C123", 6, 5.0)
        assert kwargs["id"] == "pynchy-interactive-turn-slack-C123"
        assert kwargs["task_queue"] == "pynchy-test"
        assert kwargs["id_reuse_policy"].name == "ALLOW_DUPLICATE"

    @pytest.mark.asyncio
    async def test_start_interrupted_turn_starts_dedicated_temporal_workflow(self):
        client = FakeScheduleClient()
        scheduler = SchedulerConfig(temporal_task_queue="pynchy-test")
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(scheduler)
        )
        runtime.client = client

        await runtime.start_interrupted_turn("turn-123", "admin")

        workflow, args, kwargs = client.started_workflows[0]
        assert workflow == temporal_workflows.InterruptedTurnWorkflow.run
        assert args[:2] == ("turn-123", "admin")
        assert len(args) == 4
        assert kwargs["id"] == "pynchy-interrupted-turn-turn-123"
        assert kwargs["task_queue"] == "pynchy-test"
        assert kwargs["id_reuse_policy"].name == "ALLOW_DUPLICATE"

    @pytest.mark.asyncio
    async def test_start_deploy_starts_temporal_workflow(self):
        await init_test_database()
        await initialize_deployment_state(DeployRevision("def456", "config-hash"))
        request = temporal_deploy.DeployRequest(
            chat_jid="slack:C123",
            commit_sha="abc123",
            config_hash="config-hash",
            previous_sha="def456",
            rebuild=True,
            reason="origin",
        )
        client = FakeScheduleClient()
        scheduler = SchedulerConfig(temporal_task_queue="pynchy-test")
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(scheduler)
        )
        runtime.client = client

        await runtime.start_deploy(request)

        assert len(client.started_workflows) == 1
        workflow, args, kwargs = client.started_workflows[0]
        assert workflow == temporal_workflows.DeployWorkflow.run
        payload = args[0]
        assert payload["change_kind"] == "code change"
        assert payload["commit_sha"] == "abc123"
        assert payload["config_hash"] == "config-hash"
        assert kwargs["id"] == "pynchy-deploy-abc123-config-hash"
        assert kwargs["task_queue"] == "pynchy-test"
        assert kwargs["id_reuse_policy"].name == "ALLOW_DUPLICATE"

    @pytest.mark.asyncio
    async def test_start_deploy_skips_a_revision_that_already_booted(self):
        revision = DeployRevision("abc123", "config-hash")
        await init_test_database()
        await initialize_deployment_state(revision)
        request = temporal_deploy.DeployRequest(
            chat_jid="slack:C123",
            commit_sha=revision.commit_sha,
            config_hash=revision.config_hash,
            previous_sha="def456",
            rebuild=True,
            reason="host_git_sync",
        )
        client = FakeScheduleClient()
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(SchedulerConfig())
        )
        runtime.client = client

        claim = await runtime.start_deploy(request)

        assert claim.status is DeployClaimStatus.ALREADY_APPLIED
        assert client.started_workflows == []

    @pytest.mark.asyncio
    async def test_reconcile_creates_temporal_schedule_for_recurring_agent_task(
        self, monkeypatch, temporal_task
    ):

        client = FakeScheduleClient()
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(SchedulerConfig())
        )
        runtime.client = client
        monkeypatch.setattr(
            temporal_scheduler, "get_all_tasks", AsyncMock(return_value=[temporal_task])
        )
        monkeypatch.setattr(temporal_scheduler, "get_all_host_jobs", AsyncMock(return_value=[]))
        settings = make_settings(timezone="UTC", scheduler=SchedulerConfig(), jobs={})

        await runtime.reconcile_schedules()

        schedules = {
            schedule_id: (schedule, kwargs)
            for schedule_id, schedule, kwargs in client.created_schedules
        }
        schedule, kwargs = schedules["pynchy-agent-schedule-task-with-spaces"]
        schedule_id = "pynchy-agent-schedule-task-with-spaces"
        assert schedule_id == "pynchy-agent-schedule-task-with-spaces"
        assert schedule.spec.cron_expressions == ["0 9 * * *"]
        assert schedule.spec.time_zone_name == "UTC"
        assert schedule.action.workflow == "ScheduledAgentTaskWorkflow"
        assert schedule.action.args == [temporal_task.id]
        assert schedule.action.id == "pynchy-agent-schedule-task-with-spaces-workflow"
        assert schedule.policy.overlap is ScheduleOverlapPolicy.SKIP
        assert kwargs == {}

    def test_config_job_buffers_one_overlapping_schedule_occurrence(
        self, monkeypatch, temporal_task
    ):
        """Config jobs remain serial while retaining one pending run."""
        settings = make_settings(timezone="UTC", scheduler=SchedulerConfig(), jobs={})
        temporal_task.config_job_name = "fam_daily_checkin"

        schedules = temporal_schedules.desired_recurring_schedules(
            [temporal_task], [], _scheduler_runtime()
        )
        schedule = schedules["pynchy-agent-schedule-task-with-spaces"]

        assert schedule.policy.overlap is ScheduleOverlapPolicy.BUFFER_ONE

    @pytest.mark.asyncio
    async def test_reconcile_starts_once_agent_task_as_delayed_workflow(
        self, monkeypatch, temporal_task
    ):
        temporal_task.schedule_type = "once"
        temporal_task.schedule_value = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        client = FakeScheduleClient()
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(),
            scheduler_config=_scheduler_runtime(SchedulerConfig(temporal_task_queue="pynchy-test")),
        )
        runtime.client = client
        monkeypatch.setattr(
            temporal_scheduler, "get_all_tasks", AsyncMock(return_value=[temporal_task])
        )
        monkeypatch.setattr(temporal_scheduler, "get_all_host_jobs", AsyncMock(return_value=[]))
        settings = make_settings(timezone="UTC", scheduler=runtime.scheduler_config, jobs={})

        await runtime.reconcile_schedules()

        assert all(
            schedule_id.startswith(
                (
                    "pynchy-git-sync-",
                    "pynchy-channel-reconciliation",
                    "pynchy-linear-work-item-reconciliation",
                )
            )
            for schedule_id, _schedule, _kwargs in client.created_schedules
        )
        assert len(client.started_workflows) == 1
        workflow, args, kwargs = client.started_workflows[0]
        assert workflow == temporal_workflows.ScheduledAgentTaskWorkflow.run
        assert args == (temporal_task.id,)
        assert kwargs["id"].startswith("pynchy-agent-task-task-with-spaces-")
        assert kwargs["task_queue"] == "pynchy-test"
        assert kwargs["id_reuse_policy"].name == "ALLOW_DUPLICATE_FAILED_ONLY"
        assert 0 < kwargs["start_delay"].total_seconds() <= 300

    @pytest.mark.asyncio
    async def test_reconcile_starts_resumed_once_task_exactly_once(
        self, monkeypatch, temporal_task
    ):
        temporal_task.schedule_type = "once"
        previous_workflow_id = temporal_schedules.agent_task_workflow_id(temporal_task)
        temporal_task.superseded_occurrence_due_at = temporal_task.schedule_value
        temporal_task.superseded_occurrence_generation = 0
        temporal_task.occurrence_due_at = datetime.now(UTC).isoformat()
        temporal_task.occurrence_generation = 1
        client = DeduplicatingFakeScheduleClient()
        client.workflow_ids.add(previous_workflow_id)
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(),
            scheduler_config=_scheduler_runtime(SchedulerConfig(temporal_task_queue="pynchy-test")),
        )
        runtime.client = client
        monkeypatch.setattr(
            temporal_scheduler, "get_all_tasks", AsyncMock(return_value=[temporal_task])
        )
        monkeypatch.setattr(temporal_scheduler, "get_all_host_jobs", AsyncMock(return_value=[]))
        settings = make_settings(timezone="UTC", scheduler=runtime.scheduler_config, jobs={})

        await asyncio.gather(runtime.reconcile_schedules(), runtime.reconcile_schedules())

        assert len(client.started_workflows) == 1
        _workflow, _args, kwargs = client.started_workflows[0]
        assert kwargs["id"] != previous_workflow_id
        assert kwargs["id"].endswith("-resume-1")
        assert kwargs["id_reuse_policy"].name == "ALLOW_DUPLICATE_FAILED_ONLY"

    @pytest.mark.asyncio
    async def test_reconcile_cancels_running_prior_occurrence_before_resume(
        self, monkeypatch, temporal_task
    ):
        temporal_task.schedule_type = "once"
        previous_workflow_id = temporal_schedules.agent_task_workflow_id(temporal_task)
        temporal_task.superseded_occurrence_due_at = temporal_task.schedule_value
        temporal_task.superseded_occurrence_generation = 0
        temporal_task.occurrence_due_at = datetime.now(UTC).isoformat()
        temporal_task.occurrence_generation = 1
        previous_run_id = "prior-running-occurrence"
        client = DeduplicatingFakeScheduleClient()
        client.workflow_ids.add(previous_workflow_id)
        client.workflow_executions = [
            _WorkflowListEntry(
                id=previous_workflow_id,
                run_id=previous_run_id,
                workflow_type="ScheduledAgentTaskWorkflow",
                execution_time=datetime.now(UTC),
            )
        ]
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(),
            scheduler_config=_scheduler_runtime(SchedulerConfig(temporal_task_queue="pynchy-test")),
        )
        runtime.client = client
        monkeypatch.setattr(
            temporal_scheduler, "get_all_tasks", AsyncMock(return_value=[temporal_task])
        )
        monkeypatch.setattr(temporal_scheduler, "get_all_host_jobs", AsyncMock(return_value=[]))
        settings = make_settings(timezone="UTC", scheduler=runtime.scheduler_config, jobs={})

        await runtime.reconcile_schedules()

        assert client.workflow_handles[previous_workflow_id, previous_run_id].cancelled is True
        assert client.started_workflows == []

        client.workflow_executions = []
        await asyncio.gather(runtime.reconcile_schedules(), runtime.reconcile_schedules())

        assert len(client.started_workflows) == 1
        assert client.started_workflows[0][2]["id"].endswith("-resume-1")
