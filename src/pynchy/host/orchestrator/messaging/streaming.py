"""Channel streaming and trace batching infrastructure.

Handles real-time text streaming to channels and debounce-batching of trace
messages.  Extracted from output_handler.py to keep output event dispatching
separate from channel delivery mechanics.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pynchy.async_tasks import create_background_task
from pynchy.host.orchestrator.messaging.sender import (
    UpdatingMessage,
    deliver_updating_event,
    resolve_target_jid,
)
from pynchy.logger import logger
from pynchy.plugins.api import (  # beartype resolves streaming annotations at runtime.
    Channel,
    OutboundEvent,
    OutboundEventType,
)
from pynchy.workspace.api import (
    WorkspaceProfile,
)

if TYPE_CHECKING:
    from pynchy.event_bus import Event

# ---------------------------------------------------------------------------
# OutputDeps protocol — dependency interface for output handling
# ---------------------------------------------------------------------------


@runtime_checkable
class OutputDeps(Protocol):
    """Dependencies for output handling."""

    @property
    def agent_name(self) -> str: ...

    @property
    def channels(self) -> list[Channel]: ...

    @property
    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    async def broadcast_to_channels(
        self, chat_jid: str, event: OutboundEvent, *, suppress_errors: bool = True
    ) -> None: ...

    def emit(self, event: Event) -> None: ...


# ---------------------------------------------------------------------------
# Text streaming — accumulates text deltas and pushes to channels
# ---------------------------------------------------------------------------

# Minimum interval between streaming updates to channels (seconds).
_STREAM_THROTTLE = 0.5


@dataclass
class StreamState:
    """Tracks in-progress streaming text for a single chat.

    The ``event`` field is a TEXT OutboundEvent whose content grows as deltas
    arrive.  The channel's formatter handles cursor display and internal-tag
    rendering via ``event.metadata["cursor"]``.
    """

    event: OutboundEvent
    # channel -> message_id for in-place updates
    message_ids: dict[str, str] = field(default_factory=dict)
    last_update: float = 0.0


# Per-chat streaming state, created on first text event, cleaned up on result.
stream_states: dict[str, StreamState] = {}


async def stream_text_to_channels(
    deps: OutputDeps,
    chat_jid: str,
    state: StreamState,
    *,
    final: bool = False,
) -> None:
    """Push the current OutboundEvent to channels that support update_event.

    On first call, posts a message via ``post_event``.  Subsequent calls
    update it in-place via ``update_event``.  Throttled to _STREAM_THROTTLE
    unless ``final`` is True.

    The formatter inside each channel handles cursor display and internal-tag
    rendering -- this function just sets ``metadata["cursor"]`` and delegates.
    """
    now = time.monotonic()
    if _skip_stream_delivery(state, final=final, now=now):
        return

    _prepare_stream_event(state, final=final, now=now)

    for ch, target_jid in _stream_targets(deps, chat_jid):
        await _deliver_stream_update(ch, target_jid, state)


async def finalize_active_stream(deps: OutputDeps, chat_jid: str) -> None:
    """Finalize any in-progress text stream for *chat_jid*.

    Called before trace events (tool_use, thinking) so that streamed text
    becomes its own completed message, preserving chronological interleaving
    between agent text and tool calls in the channel.
    """
    state = stream_states.pop(chat_jid, None)
    if state and state.event.content:
        await stream_text_to_channels(deps, chat_jid, state, final=True)


def _skip_stream_delivery(state: StreamState, *, final: bool, now: float) -> bool:
    if not final and (now - state.last_update) < _STREAM_THROTTLE:
        return True
    return not state.event.content and not final


def _prepare_stream_event(state: StreamState, *, final: bool, now: float) -> None:
    # Tell the formatter whether to show a cursor indicator.
    state.event.metadata["cursor"] = not final
    state.last_update = now


def _stream_targets(deps: OutputDeps, chat_jid: str) -> list[tuple[Channel, str]]:
    targets: list[tuple[Channel, str]] = []
    for ch in deps.channels:
        if not ch.is_connected():
            continue
        if not hasattr(ch, "update_event") or not hasattr(ch, "post_event"):
            continue
        target_jid = resolve_target_jid(chat_jid, ch)
        if not target_jid:
            continue
        targets.append((ch, target_jid))
    return targets


async def _deliver_stream_update(ch: Channel, target_jid: str, state: StreamState) -> None:
    ch_name = getattr(ch, "name", "?")
    msg_id = state.message_ids.get(ch_name)

    try:
        if msg_id is None:
            await _post_stream_message(ch, target_jid, state, ch_name)
            return
        await ch.update_event(target_jid, msg_id, state.event)
    except Exception as exc:  # noqa: BLE001 - stream delivery failure is scoped to the current channel update.
        logger.warning("Stream post/update failed", channel=ch_name, err=str(exc))


async def _post_stream_message(
    ch: Channel,
    target_jid: str,
    state: StreamState,
    ch_name: str,
) -> None:
    msg_id = await ch.post_event(target_jid, state.event)
    if msg_id:
        state.message_ids[ch_name] = msg_id
    else:
        logger.warning("Stream post_event returned no message_id", channel=ch_name)


# ---------------------------------------------------------------------------
# Tool trace batcher — debounce-batches and coalesces per chat JID
# ---------------------------------------------------------------------------

_DEFAULT_TRACE_COOLDOWN = 3.0
_TRACE_LEDGER_SOURCE = "agent_trace"


class TraceBatcher:
    """Buffer tool events per JID and update one message across cooldowns.

    A timer flush sends the current delta but keeps the run open. Callers close
    the run before any user-visible non-tool event so the next tool sequence
    starts a fresh remote message.
    """

    def __init__(self, deps: OutputDeps, cooldown: float = _DEFAULT_TRACE_COOLDOWN) -> None:
        self._deps = deps
        self._cooldown = cooldown
        self._buffers: dict[str, list[OutboundEvent]] = {}
        self._runs: dict[str, dict[str, UpdatingMessage]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}

    # -- public API ----------------------------------------------------------

    def enqueue(self, chat_jid: str, event: OutboundEvent) -> None:
        """Append *event* to the per-JID buffer and (re)start the timer."""
        self._buffers.setdefault(chat_jid, []).append(event)
        self._reset_timer(chat_jid)

    async def flush(self, chat_jid: str) -> None:
        """Flush pending tool traces while keeping the consecutive run open."""
        self._cancel_timer(chat_jid)
        async with self._locks.setdefault(chat_jid, asyncio.Lock()):
            await self._flush_locked(chat_jid)

    async def close(self, chat_jid: str) -> None:
        """Flush pending traces and end the editable run for *chat_jid*."""
        self._cancel_timer(chat_jid)
        async with self._locks.setdefault(chat_jid, asyncio.Lock()):
            await self._flush_locked(chat_jid)
            self._runs.pop(chat_jid, None)

    async def flush_all(self) -> None:
        """Flush and close every JID -- used during shutdown."""
        jids = set(self._buffers) | set(self._runs)
        for jid in jids:
            await self.close(jid)

    def cancel(self) -> None:
        """Cancel pending timers before discarding this batcher instance."""
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()

    # -- internals -----------------------------------------------------------

    async def _flush_locked(self, chat_jid: str) -> None:
        events = self._buffers.pop(chat_jid, [])
        if not events:
            return
        delta = OutboundEvent(
            type=OutboundEventType.TEXT,
            content="\n".join(event.content for event in events),
        )
        messages = self._runs.setdefault(chat_jid, {})
        self._runs[chat_jid] = await deliver_updating_event(
            self._deps,
            chat_jid,
            delta,
            messages,
            source=_TRACE_LEDGER_SOURCE,
        )

    def _reset_timer(self, chat_jid: str) -> None:
        self._cancel_timer(chat_jid)
        loop = asyncio.get_running_loop()
        self._timers[chat_jid] = loop.call_later(
            self._cooldown,
            lambda: create_background_task(self.flush(chat_jid), name="trace-flush"),
        )

    def _cancel_timer(self, chat_jid: str) -> None:
        timer = self._timers.pop(chat_jid, None)
        if timer is not None:
            timer.cancel()


_trace_batcher: TraceBatcher | None = None


def init_trace_batcher(deps: OutputDeps, cooldown: float = _DEFAULT_TRACE_COOLDOWN) -> None:
    """Initialise the module-level TraceBatcher. Called once at startup."""
    global _trace_batcher  # noqa: PLW0603 - process-wide singleton.
    _trace_batcher = TraceBatcher(deps, cooldown)


def get_trace_batcher() -> TraceBatcher | None:
    """Return the current TraceBatcher (or None before init)."""
    return _trace_batcher


def reset_trace_batcher() -> None:  # noqa: V103
    """Clear the process-wide trace batcher before a fresh app lifecycle."""
    global _trace_batcher  # noqa: PLW0603 - process-wide singleton.
    if _trace_batcher is not None:
        _trace_batcher.cancel()
    _trace_batcher = None


async def enqueue_tool_trace(deps: OutputDeps, chat_jid: str, event: OutboundEvent) -> None:
    """Enqueue one tool trace, or broadcast when startup has no batcher."""
    if _trace_batcher is not None:
        _trace_batcher.enqueue(chat_jid, event)
    else:
        await deps.broadcast_to_channels(chat_jid, event)


async def close_trace_run(chat_jid: str) -> None:
    """Close the current consecutive tool run, if batching is active."""
    if _trace_batcher is not None:
        await _trace_batcher.close(chat_jid)
