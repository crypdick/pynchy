"""Scheduled-task child-thread routing owned by the application shell."""

from __future__ import annotations

from pynchy.host.orchestrator.scheduled_threads import (
    add_scheduled_thread_participants,
    create_scheduled_thread,
    find_scheduled_thread,
)
from pynchy.types import (
    Channel,  # noqa: TC001, RUF100 - beartype resolves this annotation at runtime.
)


class ScheduledThreadRouting:
    """Capability facade for routing scheduled tasks to child conversations."""

    channels: list[Channel]

    async def create_scheduled_thread(
        self,
        parent_jid: str,
        name: str,
        *,
        participant_ids: tuple[str, ...] = (),
    ) -> str:
        """Create a child conversation on the channel that owns *parent_jid*."""
        return await create_scheduled_thread(
            self.channels,
            parent_jid,
            name,
            participant_ids=participant_ids,
        )

    async def find_scheduled_thread(self, parent_jid: str, name: str) -> str | None:
        """Find an active child conversation on the channel that owns *parent_jid*."""
        return await find_scheduled_thread(self.channels, parent_jid, name)

    async def add_scheduled_thread_participants(
        self,
        child_jid: str,
        participant_ids: tuple[str, ...],
    ) -> None:
        """Add participants to a reused child conversation, when the channel supports it."""
        await add_scheduled_thread_participants(self.channels, child_jid, participant_ids)
