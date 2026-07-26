"""Temporal activities for interactive message turn orchestration."""

from __future__ import annotations

from typing import cast

from temporalio import activity

from pynchy.host.orchestrator.messaging import pipeline as messaging_pipeline
from pynchy.host.orchestrator.messaging.outcomes import (  # noqa: TC001, RUF100 - beartype resolves this result annotation.
    TURN_PAUSED,
    TURN_RESET,
    ProcessGroupResult,
)
from pynchy.host.orchestrator.temporal.heartbeats import activity_heartbeats
from pynchy.host.orchestrator.temporal.runtime_state import (
    _record_activity_result,
    _require_scheduler_deps,
)
from pynchy.host.orchestrator.temporal.schedules import safe_workflow_fragment
from pynchy.host.orchestrator.temporal.workflows import (
    CONTINUE_AFTER_SAFE_INTERRUPT,
)
from pynchy.host.orchestrator.temporal.workflows import (
    TURN_PAUSED as TURN_PAUSED_RESULT,
)
from pynchy.host.orchestrator.temporal.workflows import (
    TURN_RESET as TURN_RESET_RESULT,
)

_INTERACTIVE_TURN_RETRY_REQUESTED = "Interactive message turn requested retry"


def interactive_message_workflow_id(chat_jid: str) -> str:
    """Return the reusable workflow key for one chat's interactive turn worker."""
    return f"pynchy-interactive-turn-{safe_workflow_fragment(chat_jid)}"


async def _run_message_turn_activity(chat_jid: str) -> ProcessGroupResult:
    """Run one message turn while sending the Temporal heartbeat."""
    async with activity_heartbeats(chat_jid):
        return await _process_interactive_message_turn(_require_scheduler_deps(), chat_jid)


def _activity_result(chat_jid: str, handled: ProcessGroupResult) -> str:
    """Translate one message-turn result into the workflow's next action."""
    if handled is messaging_pipeline.CONTINUE_AFTER_SAFE_INTERRUPT:
        _record_activity_result(chat_jid, CONTINUE_AFTER_SAFE_INTERRUPT)
        return CONTINUE_AFTER_SAFE_INTERRUPT
    if handled is TURN_PAUSED:
        _record_activity_result(chat_jid, TURN_PAUSED_RESULT)
        return TURN_PAUSED_RESULT
    if handled is TURN_RESET:
        _record_activity_result(chat_jid, TURN_RESET_RESULT)
        return TURN_RESET_RESULT
    if not handled:
        _record_activity_result(chat_jid, "retry_requested")
        raise RuntimeError(_INTERACTIVE_TURN_RETRY_REQUESTED)
    _record_activity_result(chat_jid, "completed")
    return "completed"


@activity.defn(name="run_interactive_message_turn")
async def run_interactive_message_turn(chat_jid: str) -> str:
    """Temporal activity that runs one interactive message turn."""
    try:
        return _activity_result(chat_jid, await _run_message_turn_activity(chat_jid))
    except Exception as exc:  # noqa: BLE001, RUF100 - allow: exception-handling; record activity failure.
        _record_activity_result(chat_jid, "error", str(exc))
        raise


async def _process_interactive_message_turn(deps: object, chat_jid: str) -> ProcessGroupResult:
    typed = cast("messaging_pipeline.MessageHandlerDeps", deps)
    return await typed.queue.run_message_turn(chat_jid)
