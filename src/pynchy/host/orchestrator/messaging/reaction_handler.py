"""Inbound reaction handling — maps emoji reactions to actions.

Users can react to messages with specific emoji to trigger actions
without sending a follow-up text message.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pynchy import utils
from pynchy.logger import logger
from pynchy.types import OutboundEvent, OutboundEventType, RuntimeId


@runtime_checkable
class ReactionDeps(Protocol):
    """Dependencies for reaction processing."""

    @property
    def workspaces(self) -> dict[str, Any]: ...

    @property
    def queue(self) -> object: ...

    async def start_interactive_turn(self, chat_jid: str) -> None: ...

    async def broadcast_to_channels(
        self, chat_jid: str, event: OutboundEvent, *, suppress_errors: bool = True
    ) -> None: ...


# Emoji → action mapping
# Eyes: re-queue message processing (retry / re-check)
# X: interrupt the active agent
_REACTION_ACTIONS = {
    "eyes": "retry",
    "x": "interrupt",
}


async def handle_reaction(
    deps: ReactionDeps,
    jid: str,
    _message_ts: str,
    _user_id: str,
    emoji: str,
) -> None:
    """Route an inbound reaction to the appropriate action."""
    action = _REACTION_ACTIONS.get(emoji)
    if not action:
        return

    group = deps.workspaces.get(jid)
    if not group:
        return

    if action == "retry":
        await deps.start_interactive_turn(jid)
        logger.info("Reaction retry", group=group.name, emoji=emoji)

    elif action == "interrupt":
        runtime_id = RuntimeId(group.folder)
        if deps.queue.is_active_task(runtime_id):
            deps.queue.clear_pending_tasks(runtime_id)

            utils.create_background_task(
                deps.queue.stop_active_process(runtime_id),
                name=f"reaction-interrupt-{jid[:20]}",
            )

            await deps.broadcast_to_channels(
                jid,
                OutboundEvent(type=OutboundEventType.SYSTEM, content="Interrupted by reaction."),
            )
            logger.info("Reaction interrupt", group=group.name, emoji=emoji)
