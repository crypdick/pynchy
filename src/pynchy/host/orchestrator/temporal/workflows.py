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
