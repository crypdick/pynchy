"""Concrete IPC dependency adapters composed by the orchestrator."""

from __future__ import annotations

from collections.abc import (
    Sequence,  # noqa: TC003, RUF100 - beartype resolves channel collections at runtime.
)
from typing import Any, Protocol, cast, runtime_checkable

from pynchy.host.orchestrator.messaging import pending_questions
from pynchy.host.orchestrator.scheduled_work_status import collect_scheduled_work
from pynchy.host.orchestrator.temporal.status import get_temporal_orchestration_states
from pynchy.state import (
    create_host_job,
    create_task,
    delete_host_job,
    delete_task,
    get_all_host_jobs,
    get_all_tasks,
    get_host_job_by_id,
    get_task_by_id,
    get_task_run_logs,
    resume_task,
    update_host_job,
    update_task,
)
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves adapter annotations at runtime.
    HostJob,
    ScheduledTask,
)


@runtime_checkable
class GroupCreationChannel(Protocol):
    """Minimal command-center channel contract for periodic-agent creation."""

    name: str

    async def create_group(self, name: str) -> str | None: ...


def command_center_channel(
    channels: Sequence[object], command_center: str
) -> GroupCreationChannel | None:
    """Return the configured command center when it can create a group."""
    return next(
        (
            cast("GroupCreationChannel", channel)
            for channel in channels
            if getattr(channel, "name", None) == command_center and hasattr(channel, "create_group")
        ),
        None,
    )


def valid_jid(value: object) -> str | None:
    """Normalize a non-empty channel group ID."""
    return value.strip() if isinstance(value, str) and value.strip() else None


class PendingQuestionStore:
    """Adapter for the application-owned pending-question persistence."""

    create = staticmethod(pending_questions.create_pending_question)
    update_message_id = staticmethod(pending_questions.update_message_id)
    resolve = staticmethod(pending_questions.resolve_pending_question)


class ScheduledWorkStore:
    """Adapter for the application-owned scheduled-work persistence."""

    create_task = staticmethod(create_task)
    create_host_job = staticmethod(create_host_job)
    get_task_by_id = staticmethod(get_task_by_id)
    get_host_job_by_id = staticmethod(get_host_job_by_id)
    update_task = staticmethod(update_task)
    update_host_job = staticmethod(update_host_job)
    resume_task = staticmethod(resume_task)
    delete_task = staticmethod(delete_task)
    delete_host_job = staticmethod(delete_host_job)


async def scheduled_work_status(
    source_group: str,
    *,
    is_admin: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return only scheduled work the requesting workspace may view."""

    async def visible_tasks() -> list[ScheduledTask]:
        tasks = await get_all_tasks()
        return tasks if is_admin else [task for task in tasks if task.group_folder == source_group]

    async def visible_host_jobs() -> list[HostJob]:
        return await get_all_host_jobs() if is_admin else []

    return await collect_scheduled_work(
        visible_tasks,
        visible_host_jobs,
        lambda task_id: get_task_run_logs(task_id, limit=5),
        get_temporal_orchestration_states,
    )
