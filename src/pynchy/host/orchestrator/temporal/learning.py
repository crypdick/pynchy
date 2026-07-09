"""Temporal learning-review activity wiring."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from temporalio import activity

from pynchy.host.learning.packet_codec import packet_from_payload
from pynchy.host.learning.packet_models import (
    LearningPacket,  # noqa: TC001, RUF100 - beartype resolves Temporal learning annotations at runtime.
)
from pynchy.host.learning.review_runner import run_learning_review as _run_learning_review_agent
from pynchy.host.orchestrator.task_scheduler import (
    SchedulerDependencies,  # noqa: TC001, RUF100 - beartype resolves Temporal learning annotations at runtime.
)
from pynchy.host.orchestrator.temporal.runtime_state import (
    _record_activity_result,
    _require_scheduler_deps,
)
from pynchy.host.orchestrator.temporal.schedules import safe_workflow_fragment
from pynchy.logger import logger

_LEARNING_REVIEW_CHAT_JID_REQUIRED = "learning reviewer run requires a chat_jid"


def learning_review_workflow_id(packet: LearningPacket) -> str:
    """Return the idempotency key for one hidden learning review."""
    return f"pynchy-learning-review-{safe_workflow_fragment(packet.job_id)}"


@activity.defn(name="run_learning_review")
async def run_learning_review(packet_payload: dict[str, Any]) -> str:
    """Temporal activity that runs one hidden Obsidian learning review."""
    packet = packet_from_payload(packet_payload)
    try:
        result = await _run_learning_review(packet, _require_scheduler_deps())
    except Exception as exc:  # noqa: BLE001, RUF100 - allow: exception-handling; record activity failure.
        _record_activity_result(packet.job_id, "error", str(exc))
        raise
    _record_activity_result(packet.job_id, result)
    return result


async def _run_learning_review(packet: LearningPacket, deps: SchedulerDependencies) -> str:
    async def run_agent_via_queue(*args: object, **kwargs: object) -> str:
        return await _run_agent_via_queue(deps, *args, **kwargs)

    return await _run_learning_review_agent(packet, run_agent_via_queue)


async def _run_agent_via_queue(
    deps: SchedulerDependencies,
    *args: object,
    **kwargs: object,
) -> str:
    loop = asyncio.get_running_loop()
    result_future: asyncio.Future[str] = loop.create_future()
    group_jid = _learning_run_group_jid(args, kwargs)

    async def run_queued_agent() -> None:
        if result_future.cancelled():
            return
        try:
            result = await deps.run_agent(*args, **kwargs)
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
        group_jid,
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


def _learning_run_group_jid(args: tuple[object, ...], kwargs: dict[str, object]) -> str:
    if len(args) >= 2 and isinstance(args[1], str):
        return args[1]

    chat_jid = kwargs.get("chat_jid")
    if isinstance(chat_jid, str):
        return chat_jid

    raise TypeError(_LEARNING_REVIEW_CHAT_JID_REQUIRED)
