"""Child-thread routing owned by the application shell."""

from __future__ import annotations

from pynchy.host.orchestrator.threads import (
    EnsuredThread,
    add_thread_participants,
    create_thread,
    ensure_thread,
    find_thread,
    supports_thread_creation,
)
from pynchy.plugins.api import (
    Channel,
)


class ThreadRouting:
    """Capability facade for routing child-thread operations to channel owners."""

    channels: list[Channel]

    async def supports_thread_creation(self, parent_jid: str) -> bool:
        """Return whether the resolved target can host a child conversation."""
        return await supports_thread_creation(self.channels, parent_jid)

    async def create_thread(
        self,
        parent_jid: str,
        name: str,
        *,
        participant_ids: tuple[str, ...] = (),
    ) -> str:
        """Create a child conversation on the channel that owns *parent_jid*."""
        return await create_thread(
            self.channels,
            parent_jid,
            name,
            participant_ids=participant_ids,
        )

    async def find_thread(self, parent_jid: str, name: str) -> str | None:
        """Find an active child conversation on the channel that owns *parent_jid*."""
        return await find_thread(self.channels, parent_jid, name)

    async def add_thread_participants(
        self,
        child_jid: str,
        participant_ids: tuple[str, ...],
    ) -> None:
        """Add participants to a child conversation, when the channel supports it."""
        await add_thread_participants(self.channels, child_jid, participant_ids)

    async def ensure_thread(
        self,
        parent_jid: str,
        name: str,
        *,
        participant_ids: tuple[str, ...] = (),
    ) -> EnsuredThread:
        """Find or create one named child conversation idempotently."""
        return await ensure_thread(
            self.channels,
            parent_jid,
            name,
            participant_ids=participant_ids,
        )
