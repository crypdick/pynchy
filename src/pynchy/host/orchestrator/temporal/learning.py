"""Temporal learning-review activity wiring."""

from __future__ import annotations

import asyncio
from collections.abc import (  # noqa: TC003, RUF100 - beartype resolves Temporal learning annotations at runtime.
    Awaitable,
    Callable,
)
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from temporalio import activity

from pynchy.host.learning.api import run_learning_review as _run_learning_review_agent
from pynchy.host.orchestrator.scheduler_deps import (
    SchedulerDependencies,  # noqa: TC001, RUF100 - beartype resolves Temporal learning annotations at runtime.
)
from pynchy.host.orchestrator.temporal.runtime_state import (
    _record_activity_result,
    _require_scheduler_deps,
)
from pynchy.host.orchestrator.temporal.schedules import safe_workflow_fragment
from pynchy.learning_packets import (
    LearningPacket,  # noqa: TC001, RUF100 - beartype resolves Temporal learning annotations at runtime.
    packet_from_payload,
)
from pynchy.logger import logger
from pynchy.workspace.api import (
    RuntimeTarget,
    WorkspaceProfile,
)

if TYPE_CHECKING:
    from pynchy.agent_protocol.api import ContainerOutput


def learning_review_workflow_id(packet: LearningPacket) -> str:
    """Return the idempotency key for one hidden learning review."""
    return f"pynchy-learning-review-{safe_workflow_fragment(packet.job_id)}"


@activity.defn(name="run_learning_review")
async def run_learning_review(packet_payload: dict[str, Any]) -> str:
    """Temporal activity that runs one hidden Obsidian learning review."""
    packet = packet_from_payload(packet_payload)
    try:
        result = await _run_learning_review(
            packet,
            cast("SchedulerDependencies", _require_scheduler_deps()),
        )
    except Exception as exc:  # noqa: BLE001, RUF100 - allow: exception-handling; record activity failure.
        _record_activity_result(packet.job_id, "error", str(exc))
        raise
    _record_activity_result(packet.job_id, result)
    return result


async def _run_learning_review(packet: LearningPacket, deps: SchedulerDependencies) -> str:
    async def run_agent_via_queue(  # noqa: PLR0913, RUF100 - callback mirrors RunAgent calls.
        group: WorkspaceProfile,
        chat_jid: str,
        messages: list[dict[str, object]],
        on_output: Callable[[ContainerOutput], Awaitable[None]] | None = None,
        extra_system_notices: list[str] | None = None,
        *,
        is_scheduled_task: bool = False,
        repo_access_override: str | None = None,
        input_source: str = "user",
    ) -> str:
        return await _run_agent_via_queue(
            deps,
            group,
            chat_jid,
            messages,
            on_output=on_output,
            extra_system_notices=extra_system_notices,
            is_scheduled_task=is_scheduled_task,
            repo_access_override=repo_access_override,
            input_source=input_source,
        )

    return await _run_learning_review_agent(packet, run_agent_via_queue)


async def _run_agent_via_queue(  # noqa: PLR0913, RUF100 - adapter mirrors SchedulerDependencies.
    deps: SchedulerDependencies,
    group: WorkspaceProfile,
    chat_jid: str,
    messages: list[dict[str, object]],
    on_output: Callable[[ContainerOutput], Awaitable[None]] | None = None,
    extra_system_notices: list[str] | None = None,
    *,
    is_scheduled_task: bool = False,
    repo_access_override: str | None = None,
    input_source: str = "user",
) -> str:
    loop = asyncio.get_running_loop()
    result_future: asyncio.Future[str] = loop.create_future()

    async def run_queued_agent() -> None:
        if result_future.cancelled():
            return
        try:
            result = await deps.run_agent(
                group,
                chat_jid,
                messages,
                on_output=on_output,
                extra_system_notices=extra_system_notices,
                is_scheduled_task=is_scheduled_task,
                repo_access_override=repo_access_override,
                input_source=input_source,
            )
        except asyncio.CancelledError:
            if not result_future.done():
                result_future.cancel()
            raise
        except Exception as exc:  # noqa: BLE001, RUF100 - allow: exception-handling; propagate queued run failure.
            logger.exception("Queued learning run failed", err=str(exc))
            if not result_future.done():
                result_future.set_exception(exc)
        else:
            if not result_future.done():
                result_future.set_result(result)

    accepted = deps.queue.enqueue_task(
        RuntimeTarget.from_workspace(group),
        f"learning-review-{uuid4().hex}",
        run_queued_agent,
    )
    if accepted is False:
        result_future.cancel()
        raise asyncio.CancelledError

    try:
        return await result_future
    except asyncio.CancelledError:
        result_future.cancel()
        raise
