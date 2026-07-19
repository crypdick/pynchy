"""Channel capability routing for scheduled task child threads."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pynchy.types import Channel  # noqa: TC001, RUF100 - beartype resolves this runtime annotation.


@runtime_checkable
class ScheduledThreadChannel(Protocol):
    """Optional channel capability for creating one child conversation."""

    async def create_thread(
        self,
        parent_jid: str,
        name: str,
        *,
        participant_ids: tuple[str, ...] = (),
    ) -> str: ...


async def create_scheduled_thread(
    channels: list[Channel],
    parent_jid: str,
    name: str,
    *,
    participant_ids: tuple[str, ...] = (),
) -> str:
    """Create a child conversation and include active parent participants."""
    channel = next(
        (
            candidate
            for candidate in channels
            if candidate.owns_jid(parent_jid) and isinstance(candidate, ScheduledThreadChannel)
        ),
        None,
    )
    if channel is None:
        raise RuntimeError(f"Channel does not support scheduled task threads: {parent_jid}")
    child_jid = await channel.create_thread(
        parent_jid,
        name,
        participant_ids=participant_ids,
    )
    if not child_jid:
        raise RuntimeError("Channel returned no JID for scheduled task thread")
    return child_jid
