"""Temporal workflow definitions for Pynchy host orchestration.

Keep this module deterministic and light: activities do the Pynchy I/O.
"""

from __future__ import annotations

from datetime import timedelta
from typing import cast

from temporalio import workflow


@workflow.defn
class ScheduledAgentTaskWorkflow:
    """Run one scheduled agent task through a host-side activity."""

    @workflow.run
    async def run(self, task_id: str) -> str:
        return cast(
            str,
            await workflow.execute_activity(
                "run_scheduled_agent_task",
                task_id,
                start_to_close_timeout=timedelta(hours=12),
            ),
        )


@workflow.defn
class DatabaseHostJobWorkflow:
    """Run one database-backed host job through a host-side activity."""

    @workflow.run
    async def run(self, job_id: str) -> str:
        return cast(
            str,
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
            str,
            await workflow.execute_activity(
                "run_config_host_cron_job",
                job_name,
                start_to_close_timeout=timedelta(hours=12),
            ),
        )
