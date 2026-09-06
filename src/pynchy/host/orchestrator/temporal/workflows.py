"""Temporal workflow definitions for Pynchy host orchestration.

Keep this module deterministic and light: activities do the Pynchy I/O.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from pynchy.turn_outcomes import TurnOutcome

ACTIVITY_HEARTBEAT_TIMEOUT_SECONDS = 30
_TERMINAL_FAILURE_ERROR_LIMIT = 1_000


def _terminal_failure_error(exc: ActivityError) -> str:
    """Keep the actionable activity cause without allowing unbounded task logs."""
    cause = exc.__cause__
    source = cause if cause is not None else exc
    return f"{type(source).__name__}: {source}"[:_TERMINAL_FAILURE_ERROR_LIMIT]


@workflow.defn
class InteractiveMessageWorkflow:
    """Own interactive turns, their retries, and notifications received mid-turn."""

    def __init__(self) -> None:
        self._pending = True

    @workflow.signal  # noqa: V105 - Temporal invokes signals by registered name.
    def request_turn(self) -> None:
        self._pending = True

    @workflow.run
    async def run(
        self,
        chat_jid: str,
        maximum_attempts: int,
        initial_retry_seconds: float,
    ) -> str:
        result = TurnOutcome.COMPLETED.value
        while self._pending:
            self._pending = False
            result = await _run_interactive_message_turn(
                chat_jid,
                maximum_attempts,
                initial_retry_seconds,
            )
            self._pending |= result == TurnOutcome.CONTINUE_AFTER_SAFE_INTERRUPT.value
        return result


async def _run_interactive_message_turn(
    chat_jid: str,
    maximum_attempts: int,
    initial_retry_seconds: float,
) -> str:
    """Execute one fresh activity for an interactive message turn."""
    return cast(
        "str",
        await workflow.execute_activity(
            "run_interactive_message_turn",
            chat_jid,
            start_to_close_timeout=timedelta(hours=12),
            heartbeat_timeout=timedelta(seconds=ACTIVITY_HEARTBEAT_TIMEOUT_SECONDS),
            retry_policy=RetryPolicy(
                maximum_attempts=maximum_attempts,
                initial_interval=timedelta(seconds=initial_retry_seconds),
                backoff_coefficient=2.0,
            ),
        ),
    )


@workflow.defn
class DeployWorkflow:
    """Run one deploy handoff through a host-side activity."""

    @workflow.run
    async def run(self, deploy_payload: dict[str, Any]) -> str:
        return cast(
            "str",
            await workflow.execute_activity(
                "run_deploy",
                deploy_payload,
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=RetryPolicy(maximum_attempts=1),
            ),
        )


@workflow.defn
class InterruptedTurnWorkflow:
    """Resume one durable agent-turn checkpoint after its worker was interrupted."""

    @workflow.run
    async def run(
        self,
        turn_id: str,
        group_folder: str,
        maximum_attempts: int,
        initial_retry_seconds: float,
    ) -> str:
        result = cast(
            "str",
            await workflow.execute_activity(
                "run_interrupted_agent_turn",
                turn_id,
                start_to_close_timeout=timedelta(hours=12),
                heartbeat_timeout=timedelta(seconds=ACTIVITY_HEARTBEAT_TIMEOUT_SECONDS),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    initial_interval=timedelta(seconds=5),
                    backoff_coefficient=2.0,
                ),
            ),
        )
        while result == TurnOutcome.CONTINUE_AFTER_SAFE_INTERRUPT.value:
            result = await _run_interactive_runtime_turn(
                group_folder,
                maximum_attempts,
                initial_retry_seconds,
            )
        return result


async def _run_interactive_runtime_turn(
    group_folder: str,
    maximum_attempts: int,
    initial_retry_seconds: float,
) -> str:
    """Execute one continuation after resolving the runtime's current address."""
    return cast(
        "str",
        await workflow.execute_activity(
            "run_interactive_runtime_turn",
            group_folder,
            start_to_close_timeout=timedelta(hours=12),
            heartbeat_timeout=timedelta(seconds=ACTIVITY_HEARTBEAT_TIMEOUT_SECONDS),
            retry_policy=RetryPolicy(
                maximum_attempts=maximum_attempts,
                initial_interval=timedelta(seconds=initial_retry_seconds),
                backoff_coefficient=2.0,
            ),
        ),
    )


@workflow.defn
class HostGitSyncWorkflow:
    """Run one host repository sync poll through a host-side activity."""

    @workflow.run
    async def run(self) -> str:
        return cast(
            "str",
            await workflow.execute_activity(
                "run_host_git_sync",
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=1),
            ),
        )


@workflow.defn
class ExternalGitSyncWorkflow:
    """Run one external repository sync poll through a host-side activity."""

    @workflow.run
    async def run(self, repo_slug: str) -> str:
        return cast(
            "str",
            await workflow.execute_activity(
                "run_external_git_sync",
                repo_slug,
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=1),
            ),
        )


@workflow.defn
class ChannelReconciliationWorkflow:
    """Run one channel reconciliation pass through a host-side activity."""

    @workflow.run
    async def run(self) -> str:
        return cast(
            "str",
            await workflow.execute_activity(
                "run_channel_reconciliation",
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=1),
            ),
        )


@workflow.defn
class LinearWorkItemReconciliationWorkflow:
    """Repair missing or orphaned managed Linear execution tasks."""

    @workflow.run
    async def run(self) -> str:
        return cast(
            "str",
            await workflow.execute_activity(
                "run_linear_work_item_reconciliation",
                start_to_close_timeout=timedelta(hours=12),
                retry_policy=RetryPolicy(maximum_attempts=1),
            ),
        )


@workflow.defn
class LinearPlanReviewWorkflow:
    """Review one immutable Human Approved issue revision."""

    @workflow.run
    async def run(self, admission: dict[str, Any]) -> str:
        return cast(
            "str",
            await workflow.execute_activity(
                "run_linear_plan_review_admission",
                admission,
                start_to_close_timeout=timedelta(hours=12),
                heartbeat_timeout=timedelta(seconds=ACTIVITY_HEARTBEAT_TIMEOUT_SECONDS),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    initial_interval=timedelta(seconds=5),
                    backoff_coefficient=2.0,
                ),
            ),
        )


@workflow.defn
class CanaryRunWorkflow:
    """Run every declared external-service canary through one host activity."""

    @workflow.run
    async def run(self) -> str:
        return cast(
            "str",
            await workflow.execute_activity(
                "run_scheduled_canaries",
                start_to_close_timeout=timedelta(hours=12),
                retry_policy=RetryPolicy(maximum_attempts=1),
            ),
        )


@workflow.defn
class ScheduledAgentTaskWorkflow:
    """Run one scheduled agent task through a host-side activity."""

    @workflow.run
    async def run(self, task_id: str) -> str:
        try:
            return cast(
                "str",
                await workflow.execute_activity(
                    "run_scheduled_agent_task",
                    task_id,
                    start_to_close_timeout=timedelta(hours=12),
                    heartbeat_timeout=timedelta(seconds=ACTIVITY_HEARTBEAT_TIMEOUT_SECONDS),
                    retry_policy=RetryPolicy(
                        maximum_attempts=3,
                        initial_interval=timedelta(seconds=5),
                        backoff_coefficient=2.0,
                    ),
                ),
            )
        except ActivityError as exc:
            # Activity retries reuse the checkpoint. Once Temporal exhausts
            # them, record terminal evidence before removing only an unclaimed
            # checkpoint so a concurrent restart-recovery workflow retains ownership.
            info = workflow.info()
            await workflow.execute_activity(
                "record_terminal_scheduled_task_failure",
                {
                    "task_id": task_id,
                    "workflow_id": info.workflow_id,
                    "workflow_run_id": info.run_id,
                    "error": _terminal_failure_error(exc),
                },
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            await workflow.execute_activity(
                "clear_terminal_scheduled_turn",
                task_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise


@workflow.defn
class DatabaseHostJobWorkflow:
    """Run one database-backed host job through a host-side activity."""

    @workflow.run
    async def run(self, job_id: str) -> str:
        return cast(
            "str",
            await workflow.execute_activity(
                "run_database_host_job",
                job_id,
                start_to_close_timeout=timedelta(hours=12),
                # A host-job command can already have made an external change
                # before it returns a nonzero exit status. The next scheduled
                # occurrence is the retry boundary; retrying this activity
                # would repeat that change without an operator decision.
                retry_policy=RetryPolicy(maximum_attempts=1),
            ),
        )


@workflow.defn
class ConfigHostCronWorkflow:
    """Run one config-backed host cron job through a host-side activity."""

    @workflow.run
    async def run(self, job_name: str) -> str:
        return cast(
            "str",
            await workflow.execute_activity(
                "run_config_host_cron_job",
                job_name,
                start_to_close_timeout=timedelta(hours=12),
                # A shell command may have changed external state before it
                # fails or the worker stops. The next scheduled occurrence is
                # the retry boundary, just as it is for database host jobs.
                retry_policy=RetryPolicy(maximum_attempts=1),
            ),
        )


@workflow.defn
class LearningReviewWorkflow:
    """Run one hidden Obsidian learning review through a host-side activity."""

    @workflow.run
    async def run(self, packet_payload: dict[str, Any], maximum_attempts: int) -> str:
        return cast(
            "str",
            await workflow.execute_activity(
                "run_learning_review",
                packet_payload,
                start_to_close_timeout=timedelta(hours=12),
                retry_policy=RetryPolicy(maximum_attempts=maximum_attempts),
            ),
        )
