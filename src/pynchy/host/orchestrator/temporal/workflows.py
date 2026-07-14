"""Temporal workflow definitions for Pynchy host orchestration.

Keep this module deterministic and light: activities do the Pynchy I/O.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

from temporalio import workflow
from temporalio.common import RetryPolicy

ACTIVITY_HEARTBEAT_TIMEOUT_SECONDS = 30


@workflow.defn
class InteractiveMessageWorkflow:
    """Run one interactive message turn through a host-side activity."""

    @workflow.run
    async def run(
        self,
        chat_jid: str,
        maximum_attempts: int,
        initial_retry_seconds: float,
    ) -> str:
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
    async def run(self, turn_id: str) -> str:
        return cast(
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
class ScheduledAgentTaskWorkflow:
    """Run one scheduled agent task through a host-side activity."""

    @workflow.run
    async def run(self, task_id: str) -> str:
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
