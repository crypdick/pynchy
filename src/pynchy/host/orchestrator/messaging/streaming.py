"""Channel streaming and trace batching infrastructure.

Handles real-time text streaming to channels and debounce-batching of trace
messages.  Extracted from output_handler.py to keep output event dispatching
separate from channel delivery mechanics.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from pynchy.host.orchestrator.messaging.sender import resolve_target_jid
from pynchy.logger import logger
from pynchy.utils import create_background_task

if TYPE_CHECKING:
    from pynchy.types import Channel, OutboundEvent


# ---------------------------------------------------------------------------
# OutputDeps protocol — dependency interface for output handling
# ---------------------------------------------------------------------------


class OutputDeps(Protocol):
    """Dependencies for output handling."""

    @property
    def channels(self) -> list[Channel]: ...

    async def broadcast_to_channels(
        self, chat_jid: str, event: OutboundEvent, *, suppress_errors: bool = True
    ) -> None: ...

    def emit(self, event: Any) -> None: ...


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

    On first call, posts a new message via ``post_event``.  Subsequent calls
    update it in-place via ``update_event``.  Throttled to _STREAM_THROTTLE
    unless ``final`` is True.

    The formatter inside each channel handles cursor display and internal-tag
    rendering -- this function just sets ``metadata["cursor"]`` and delegates.
    """
    now = time.monotonic()
    if not final and (now - state.last_update) < _STREAM_THROTTLE:
        return

    if not state.event.content and not final:
        return  # nothing to show yet

    # Tell the formatter whether to show a cursor indicator.
    state.event.metadata["cursor"] = not final
    state.last_update = now

    for ch in deps.channels:
        if not ch.is_connected():
            continue
        if not hasattr(ch, "update_event") or not hasattr(ch, "post_event"):
            continue

        target_jid = resolve_target_jid(chat_jid, ch)
        if not target_jid:
            continue

        ch_name = getattr(ch, "name", "?")
        msg_id = state.message_ids.get(ch_name)

        try:
            if msg_id is None:
                msg_id = await ch.post_event(target_jid, state.event)
                if msg_id:
                    state.message_ids[ch_name] = msg_id
                else:
                    logger.warning("Stream post_event returned no message_id", channel=ch_name)
            else:
                await ch.update_event(target_jid, msg_id, state.event)
        except Exception as exc:
            logger.warning("Stream post/update failed", channel=ch_name, err=str(exc))


async def finalize_active_stream(deps: OutputDeps, chat_jid: str) -> None:
    """Finalize any in-progress text stream for *chat_jid*.

    Called before trace events (tool_use, thinking) so that streamed text
    becomes its own completed message, preserving chronological interleaving
    between agent text and tool calls in the channel.
    """
    state = stream_states.pop(chat_jid, None)
    if state and state.event.content:
        await stream_text_to_channels(deps, chat_jid, state, final=True)


# ---------------------------------------------------------------------------
# Trace batcher — debounce-batches trace messages per chat JID
# ---------------------------------------------------------------------------

_DEFAULT_TRACE_COOLDOWN = 3.0


class TraceBatcher:
    """Buffers OutboundEvent objects per JID and flushes after a cooldown.

    Result/host messages bypass the batcher entirely; callers should
    ``await flush(chat_jid)`` before sending a result so traces always
    appear before the bot reply.
    """

    def __init__(self, deps: OutputDeps, cooldown: float = _DEFAULT_TRACE_COOLDOWN) -> None:
        self._deps = deps
        self._cooldown = cooldown
        self._buffers: dict[str, list[OutboundEvent]] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}

    # -- public API ----------------------------------------------------------

    def enqueue(self, chat_jid: str, event: OutboundEvent) -> None:
        """Append *event* to the per-JID buffer and (re)start the timer."""
        self._buffers.setdefault(chat_jid, []).append(event)
        self._reset_timer(chat_jid)

    async def flush(self, chat_jid: str) -> None:
        """Flush pending traces for *chat_jid* immediately."""
        from pynchy.types import OutboundEvent, OutboundEventType

        self._cancel_timer(chat_jid)
        events = self._buffers.pop(chat_jid, [])
        if events:
            # Join buffered trace events into a single TEXT event for broadcast.
            # Each event's content is already rendered with emoji prefixes by the
            # router; we concatenate them with newlines.
            combined = OutboundEvent(
                type=OutboundEventType.TEXT,
                content="\n".join(e.content for e in events),
            )
            await self._deps.broadcast_to_channels(chat_jid, combined)

    async def flush_all(self) -> None:
        """Flush every JID -- used during shutdown."""
        jids = list(self._buffers)
        for jid in jids:
            await self.flush(jid)

    # -- internals -----------------------------------------------------------

    def _reset_timer(self, chat_jid: str) -> None:
        self._cancel_timer(chat_jid)
        loop = asyncio.get_running_loop()
        self._timers[chat_jid] = loop.call_later(
            self._cooldown,
            lambda jid=chat_jid: create_background_task(self.flush(jid), name="trace-flush"),
        )

    def _cancel_timer(self, chat_jid: str) -> None:
        timer = self._timers.pop(chat_jid, None)
        if timer is not None:
            timer.cancel()


# Module-level singleton
_trace_batcher: TraceBatcher | None = None


def init_trace_batcher(deps: OutputDeps, cooldown: float = _DEFAULT_TRACE_COOLDOWN) -> None:
    """Initialise the module-level TraceBatcher. Called once at startup."""
    global _trace_batcher
    _trace_batcher = TraceBatcher(deps, cooldown)


def get_trace_batcher() -> TraceBatcher | None:
    """Return the current TraceBatcher (or None before init)."""
    return _trace_batcher


async def enqueue_or_broadcast(deps: OutputDeps, chat_jid: str, event: OutboundEvent) -> None:
    """Enqueue via batcher if available, otherwise broadcast directly."""
    if _trace_batcher is not None:
        _trace_batcher.enqueue(chat_jid, event)
    else:
        await deps.broadcast_to_channels(chat_jid, event)
