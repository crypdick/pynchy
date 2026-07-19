"""Channel capability routing for child conversations."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pynchy.types import Channel  # noqa: TC001, RUF100 - beartype resolves this runtime annotation.


@runtime_checkable
class ThreadChannel(Protocol):
    """Optional channel capability for creating one child conversation."""

    async def create_thread(
        self,
        parent_jid: str,
        name: str,
        *,
        participant_ids: tuple[str, ...] = (),
    ) -> str: ...


@runtime_checkable
class ThreadLookupChannel(Protocol):
    """Optional channel capability for finding an active child conversation."""

    async def find_thread(self, parent_jid: str, name: str) -> str | None: ...


@runtime_checkable
class ThreadParticipantChannel(Protocol):
    """Optional channel capability for adding people to a child conversation."""

    async def add_thread_participants(
        self,
        child_jid: str,
        participant_ids: tuple[str, ...],
    ) -> None: ...


async def create_thread(
    channels: list[Channel],
    parent_jid: str,
    name: str,
    *,
    participant_ids: tuple[str, ...] = (),
) -> str:
    """Create a child conversation on the channel that owns its parent."""
    channel = next(
        (
            candidate
            for candidate in channels
            if candidate.owns_jid(parent_jid) and isinstance(candidate, ThreadChannel)
        ),
        None,
    )
    if channel is None:
        raise RuntimeError(f"Channel does not support thread creation: {parent_jid}")
    child_jid = await channel.create_thread(
        parent_jid,
        name,
        participant_ids=participant_ids,
    )
    if not child_jid:
        raise RuntimeError("Channel returned no JID for child thread")
    return child_jid


async def find_thread(
    channels: list[Channel],
    parent_jid: str,
    name: str,
) -> str | None:
    """Find an active child conversation with an exact name, if supported."""
    channel = next(
        (
            candidate
            for candidate in channels
            if candidate.owns_jid(parent_jid) and isinstance(candidate, ThreadLookupChannel)
        ),
        None,
    )
    if channel is None:
        return None
    return await channel.find_thread(parent_jid, name)


async def add_thread_participants(
    channels: list[Channel],
    child_jid: str,
    participant_ids: tuple[str, ...],
) -> None:
    """Add participants to an existing child conversation, when supported."""
    if not participant_ids:
        return
    channel = next(
        (
            candidate
            for candidate in channels
            if candidate.owns_jid(child_jid) and isinstance(candidate, ThreadParticipantChannel)
        ),
        None,
    )
    if channel is not None:
        await channel.add_thread_participants(child_jid, participant_ids)
