"""Channel capability routing for child conversations."""

from __future__ import annotations

from dataclasses import dataclass
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


@runtime_checkable
class ThreadLifecycleChannel(Protocol):
    """Optional channel capability for opening or closing a child conversation."""

    async def set_thread_closed(self, child_jid: str, *, closed: bool) -> None: ...


@dataclass(frozen=True)
class EnsuredThread:
    """Result of idempotently resolving one named child conversation."""

    jid: str | None
    created: bool


def supports_thread_lookup(channels: list[Channel], parent_jid: str) -> bool:
    """Return whether the parent channel can look up existing child threads."""
    return any(
        candidate.owns_jid(parent_jid) and isinstance(candidate, ThreadLookupChannel)
        for candidate in channels
    )


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


async def set_thread_closed(
    channels: list[Channel],
    child_jid: str,
    *,
    closed: bool,
) -> None:
    """Apply provider-neutral closed state through the child channel owner."""
    channel = next(
        (
            candidate
            for candidate in channels
            if candidate.owns_jid(child_jid) and isinstance(candidate, ThreadLifecycleChannel)
        ),
        None,
    )
    if channel is None:
        raise RuntimeError(f"Channel does not support thread lifecycle: {child_jid}")
    await channel.set_thread_closed(child_jid, closed=closed)


async def ensure_thread(
    channels: list[Channel],
    parent_jid: str,
    name: str,
    *,
    participant_ids: tuple[str, ...] = (),
    dry_run: bool = False,
) -> EnsuredThread:
    """Find or create one named child conversation without duplicating it."""
    if not supports_thread_lookup(channels, parent_jid):
        raise RuntimeError(f"Channel does not support thread lookup: {parent_jid}")

    child_jid = await find_thread(channels, parent_jid, name)
    if child_jid is not None:
        await add_thread_participants(channels, child_jid, participant_ids)
        return EnsuredThread(jid=child_jid, created=False)
    if dry_run:
        return EnsuredThread(jid=None, created=True)
    return EnsuredThread(
        jid=await create_thread(
            channels,
            parent_jid,
            name,
            participant_ids=participant_ids,
        ),
        created=True,
    )
