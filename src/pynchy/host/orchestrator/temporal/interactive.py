"""Temporal activities for interactive message turn orchestration."""

from __future__ import annotations

from temporalio import activity

from pynchy.host.orchestrator.temporal.runtime_state import (
    _record_activity_result,
    _require_scheduler_deps,
)
from pynchy.host.orchestrator.temporal.schedules import safe_workflow_fragment

_INTERACTIVE_TURN_RETRY_REQUESTED = "Interactive message turn requested retry"


def interactive_message_workflow_id(chat_jid: str) -> str:
    """Return the reusable workflow key for one chat's interactive turn worker."""
    return f"pynchy-interactive-turn-{safe_workflow_fragment(chat_jid)}"


@activity.defn(name="run_interactive_message_turn")
async def run_interactive_message_turn(chat_jid: str) -> str:
    """Temporal activity that runs one interactive message turn."""
    try:
        handled = await _process_interactive_message_turn(_require_scheduler_deps(), chat_jid)
        if not handled:
            _record_activity_result(chat_jid, "retry_requested")
            raise RuntimeError(_INTERACTIVE_TURN_RETRY_REQUESTED)
    except Exception as exc:  # noqa: BLE001, RUF100 - allow: exception-handling; record activity failure.
        _record_activity_result(chat_jid, "error", str(exc))
        raise
    _record_activity_result(chat_jid, "completed")
    return "completed"


async def _process_interactive_message_turn(deps: object, chat_jid: str) -> bool:
    from pynchy.host.orchestrator.messaging.pipeline import process_group_messages

    return await process_group_messages(deps, chat_jid)
