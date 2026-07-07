"""Background worker for hidden Obsidian learning review jobs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pynchy.config import get_settings
from pynchy.host.learning.paths import resolve_learning_paths
from pynchy.host.learning.queue import LearningQueue
from pynchy.host.learning.reviewer import build_review_prompt, should_review
from pynchy.logger import logger
from pynchy.types import WorkspaceProfile


@dataclass(frozen=True)
class LearningWorkerDeps:
    run_agent: Callable[..., Awaitable[str]]
    queue: LearningQueue


async def process_one_learning_job(deps: LearningWorkerDeps) -> bool:
    deps.queue.requeue_expired()
    claimed = deps.queue.claim_next()
    if claimed is None:
        return False

    try:
        if not should_review(claimed.packet):
            deps.queue.complete(claimed)
            return True

        paths = resolve_learning_paths(
            claimed.packet.group_folder,
            profile_override=claimed.packet.profile,
        )
        if paths is None:
            deps.queue.fail(
                claimed,
                "learning paths unavailable for "
                f"group {claimed.packet.group_folder!r} profile {claimed.packet.profile!r}",
            )
            return True

        reviewer_output: list[Any] = []

        async def on_output(output: Any) -> None:
            reviewer_output.append(output)

        reviewer_jid = f"learning-review:{paths.profile_slug}"
        result = await deps.run_agent(
            WorkspaceProfile(
                jid=reviewer_jid,
                name="Learning Reviewer",
                folder=f"learning-review-{paths.profile_slug}",
                trigger="",
                is_admin=False,
            ),
            reviewer_jid,
            [{"role": "user", "content": build_review_prompt(claimed.packet, paths)}],
            on_output=on_output,
            extra_system_notices=None,
            is_scheduled_task=True,
            repo_access_override=None,
            input_source="user",
        )
        if result == "success":
            deps.queue.complete(claimed)
            return True

        deps.queue.fail(claimed, f"learning reviewer returned {result!r}")
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # allow: exception-handling - retry learning reviewer failures
        deps.queue.fail(claimed, _failure_reason(exc))
        return True


async def start_learning_worker_loop(deps: LearningWorkerDeps) -> None:
    while True:
        try:
            processed = await process_one_learning_job(deps)
            if not processed:
                await asyncio.sleep(get_settings().learning.queue_poll_interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:  # allow: exception-handling - keep background learning worker alive
            logger.exception("Error in learning worker loop")
            await asyncio.sleep(get_settings().learning.queue_poll_interval_seconds)


def _failure_reason(exc: Exception) -> str:
    message = str(exc)
    if message:
        return f"learning reviewer failed: {message}"
    return f"learning reviewer failed: {type(exc).__name__}"
