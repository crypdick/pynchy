"""Inbound reaction handling — maps emoji reactions to actions.

Users can react to messages with specific emoji to trigger actions
without sending a follow-up text message.
"""

from __future__ import annotations

from typing import Any, Protocol

from pynchy.logger import logger


class ReactionDeps(Protocol):
    """Dependencies for reaction processing."""

    @property
    def registered_groups(self) -> dict[str, Any]: ...

    @property
    def queue(self) -> Any: ...

    async def broadcast_to_channels(
        self, chat_jid: str, text: str, *, suppress_errors: bool = True
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
    message_ts: str,  # noqa: ARG001
    user_id: str,  # noqa: ARG001
    emoji: str,
) -> None:
    """Route an inbound reaction to the appropriate action."""
    action = _REACTION_ACTIONS.get(emoji)
    if not action:
        return

    group = deps.registered_groups.get(jid)
    if not group:
        return

    if action == "retry":
        deps.queue.enqueue_message_check(jid)
        logger.info("Reaction retry", group=group.name, emoji=emoji)

    elif action == "interrupt":
        if deps.queue.is_active_task(jid):
            deps.queue.clear_pending_tasks(jid)
            from pynchy.utils import create_background_task

            create_background_task(
                deps.queue.stop_active_process(jid),
                name=f"reaction-interrupt-{jid[:20]}",
            )
            await deps.broadcast_to_channels(jid, "Interrupted by reaction.")
            logger.info("Reaction interrupt", group=group.name, emoji=emoji)
