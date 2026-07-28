"""Temporal activities for interactive message turn orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from pynchy.host.orchestrator.scheduler_deps import SchedulerDependencies

from temporalio import activity

from pynchy.host.orchestrator.temporal.heartbeats import activity_heartbeats
from pynchy.host.orchestrator.temporal.runtime_state import (
    _record_activity_result,
    _require_scheduler_deps,
    settle_turn_activity,
)
from pynchy.host.orchestrator.temporal.schedules import safe_workflow_fragment
from pynchy.turn_outcomes import TurnOutcome
from pynchy.workspace.api import RuntimeTarget

_INTERACTIVE_TURN_RETRY_REQUESTED = "Interactive message turn requested retry"


def interactive_message_workflow_id(chat_jid: str) -> str:
    """Return the reusable workflow key for one chat's interactive turn worker."""
    return f"pynchy-interactive-turn-{safe_workflow_fragment(chat_jid)}"


async def _run_message_turn_activity(chat_jid: str) -> TurnOutcome:
    """Run one message turn while sending the Temporal heartbeat."""
    deps = cast("SchedulerDependencies", _require_scheduler_deps())
    async with activity_heartbeats(chat_jid):
        await deps.startup_readiness.wait()
        return await _process_interactive_message_turn(deps, chat_jid)


async def _run_runtime_turn_activity(group_folder: str) -> TurnOutcome:
    """Resolve the runtime's current chat binding for a continuation turn."""
    deps = cast("SchedulerDependencies", _require_scheduler_deps())
    async with activity_heartbeats(group_folder):
        await deps.startup_readiness.wait()
        return await _process_interactive_runtime_turn(
            deps,
            group_folder,
        )


@activity.defn(name="run_interactive_message_turn")
async def run_interactive_message_turn(chat_jid: str) -> str:
    """Temporal activity that runs one interactive message turn."""
    try:
        outcome = await _run_message_turn_activity(chat_jid)
    except Exception as exc:  # allow: exception-handling; record activity failure.
        _record_activity_result(chat_jid, "error", str(exc))
        raise
    return settle_turn_activity(
        chat_jid,
        outcome,
        retry_error=_INTERACTIVE_TURN_RETRY_REQUESTED,
    )


@activity.defn(name="run_interactive_runtime_turn")
async def run_interactive_runtime_turn(group_folder: str) -> str:
    """Run pending messages against a stable runtime's current address."""
    try:
        outcome = await _run_runtime_turn_activity(group_folder)
    except Exception as exc:  # allow: exception-handling; record activity failure.
        _record_activity_result(group_folder, "error", str(exc))
        raise
    return settle_turn_activity(
        group_folder,
        outcome,
        retry_error=_INTERACTIVE_TURN_RETRY_REQUESTED,
    )


async def _process_interactive_message_turn(deps: object, chat_jid: str) -> TurnOutcome:
    typed = cast("SchedulerDependencies", deps)
    workspace = typed.workspaces.get(chat_jid)
    if workspace is None:
        return TurnOutcome.COMPLETED
    return await typed.queue.run_message_turn(RuntimeTarget.from_workspace(workspace))


async def _process_interactive_runtime_turn(
    deps: object,
    group_folder: str,
) -> TurnOutcome:
    typed = cast("SchedulerDependencies", deps)
    workspace = next(
        (candidate for candidate in typed.workspaces.values() if candidate.folder == group_folder),
        None,
    )
    if workspace is None:
        raise RuntimeError(f"Interactive runtime no longer exists: {group_folder}")
    return await typed.queue.run_message_turn(RuntimeTarget.from_workspace(workspace))
