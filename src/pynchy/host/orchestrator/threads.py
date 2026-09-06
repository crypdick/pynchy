"""Channel capability routing for child conversations."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from pynchy.plugins.api import (
    Channel,
)

type ThreadKind = Literal["issue", "automation", "planning", "testing", "topic"]


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
class ThreadCreationProbeChannel(Protocol):
    """Optional target-specific child-thread capability probe."""

    async def supports_child_threads(self, parent_jid: str) -> bool: ...


@runtime_checkable
class ThreadLookupChannel(Protocol):
    """Optional channel capability for finding an active child conversation."""

    async def find_thread(self, parent_jid: str, name: str) -> str | None: ...


@runtime_checkable
class ConversationExistenceChannel(Protocol):
    """Optional channel capability for checking one provider conversation."""

    async def conversation_exists(self, jid: str) -> bool: ...


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


@runtime_checkable
class ThreadKindChannel(Protocol):
    """Optional channel capability for assigning a child-conversation kind."""

    async def set_thread_kind(self, child_jid: str, kind: str) -> None: ...


@runtime_checkable
class ThreadTitleChannel(Protocol):
    """Optional channel capability for updating a child-conversation title."""

    async def set_thread_title(self, child_jid: str, title: str) -> None: ...


@runtime_checkable
class ThreadPinnedLinkChannel(Protocol):
    """Optional channel capability for pinning one canonical thread link."""

    async def ensure_thread_link_pinned(self, child_jid: str, url: str) -> None: ...


@runtime_checkable
class ForumGuidelinesChannel(Protocol):
    """Optional channel capability for reconciling a forum's managed guidelines."""

    async def ensure_forum_guidelines_linked(self, parent_jid: str, url: str) -> None: ...


@runtime_checkable
class _ThreadTitleOwner(Protocol):
    def owns_jid(self, jid: str) -> bool: ...


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


async def supports_thread_creation(channels: list[Channel], parent_jid: str) -> bool:
    """Return whether this specific target can host a child conversation."""
    channel = next(
        (
            candidate
            for candidate in channels
            if candidate.owns_jid(parent_jid) and isinstance(candidate, ThreadChannel)
        ),
        None,
    )
    if channel is None:
        return False
    if isinstance(channel, ThreadCreationProbeChannel):
        return await channel.supports_child_threads(parent_jid)
    return True


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


async def provider_conversation_exists(channels: list[Channel], jid: str) -> bool | None:
    """Return provider presence when the owning channel can prove it."""
    channel = next(
        (
            candidate
            for candidate in channels
            if candidate.owns_jid(jid) and isinstance(candidate, ConversationExistenceChannel)
        ),
        None,
    )
    if channel is None:
        return None
    return await channel.conversation_exists(jid)


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


async def set_thread_kind(
    channels: list[Channel],
    child_jid: str,
    kind: ThreadKind,
) -> None:
    """Apply a semantic child kind when the owning channel supports it."""
    channel = next(
        (
            candidate
            for candidate in channels
            if candidate.owns_jid(child_jid) and isinstance(candidate, ThreadKindChannel)
        ),
        None,
    )
    if channel is not None:
        await channel.set_thread_kind(child_jid, kind)


async def set_thread_title(
    channels: Sequence[_ThreadTitleOwner],
    child_jid: str,
    title: str,
) -> None:
    """Update a child title when the owning channel supports it."""
    channel = next(
        (
            candidate
            for candidate in channels
            if candidate.owns_jid(child_jid) and isinstance(candidate, ThreadTitleChannel)
        ),
        None,
    )
    if channel is not None:
        await channel.set_thread_title(child_jid, title)


async def ensure_thread_link_pinned(
    channels: Sequence[_ThreadTitleOwner],
    child_jid: str,
    url: str,
) -> None:
    """Pin one managed link when the child channel supports pinned links."""
    channel = next(
        (
            candidate
            for candidate in channels
            if candidate.owns_jid(child_jid) and isinstance(candidate, ThreadPinnedLinkChannel)
        ),
        None,
    )
    if channel is not None:
        await channel.ensure_thread_link_pinned(child_jid, url)


async def ensure_forum_guidelines_linked(
    channels: Sequence[_ThreadTitleOwner],
    parent_jid: str,
    url: str,
) -> None:
    """Reconcile a managed forum-project link when the channel supports it."""
    channel = next(
        (
            candidate
            for candidate in channels
            if candidate.owns_jid(parent_jid) and isinstance(candidate, ForumGuidelinesChannel)
        ),
        None,
    )
    if channel is not None:
        await channel.ensure_forum_guidelines_linked(parent_jid, url)


async def ensure_thread(  # noqa: PLR0913 - one entry point owns all thread creation options.
    channels: list[Channel],
    parent_jid: str,
    name: str,
    *,
    participant_ids: tuple[str, ...] = (),
    kind: ThreadKind = "topic",
    dry_run: bool = False,
) -> EnsuredThread:
    """Find or create one named child conversation without duplicating it."""
    if not supports_thread_lookup(channels, parent_jid):
        raise RuntimeError(f"Channel does not support thread lookup: {parent_jid}")

    child_jid = await find_thread(channels, parent_jid, name)
    if child_jid is not None:
        await add_thread_participants(channels, child_jid, participant_ids)
        await set_thread_kind(channels, child_jid, kind)
        return EnsuredThread(jid=child_jid, created=False)
    if dry_run:
        return EnsuredThread(jid=None, created=True)
    child_jid = await create_thread(
        channels,
        parent_jid,
        name,
        participant_ids=participant_ids,
    )
    await set_thread_kind(channels, child_jid, kind)
    return EnsuredThread(jid=child_jid, created=True)
