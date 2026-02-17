"""Streamed output handling — processes container output and broadcasts to channels.

Extracted from app.py to keep the orchestrator focused on wiring.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count
from typing import TYPE_CHECKING, Any, Protocol

from pynchy.config import get_settings
from pynchy.db import store_message_direct
from pynchy.event_bus import AgentTraceEvent, MessageEvent
from pynchy.logger import logger
from pynchy.messaging.bus import finalize_stream_or_broadcast
from pynchy.messaging.router import format_tool_preview, parse_host_tag

if TYPE_CHECKING:
    from pynchy.types import Channel, ContainerOutput, RegisteredGroup

_trace_counter = count(1)

# Tool names whose tool_result content should be broadcast in full
# instead of the generic "📋 tool result" placeholder.
_VERBOSE_RESULT_TOOLS = frozenset({"ExitPlanMode", "EnterPlanMode"})

# Tracks the last tool_use name per chat so we can enrich the subsequent tool_result.
_last_tool_name: dict[str, str] = {}

# Channel broadcast truncation threshold for tool results.
# Full content is always persisted to DB; only the channel broadcast is truncated.
_MAX_TOOL_OUTPUT = 4000

# Minimum interval between streaming updates to channels (seconds).
_STREAM_THROTTLE = 0.5


@dataclass
class _StreamState:
    """Tracks in-progress streaming text for a single chat."""

    buffer: str = ""
    # channel → message_id for in-place updates
    message_ids: dict[str, str] = field(default_factory=dict)
    last_update: float = 0.0


# Per-chat streaming state, created on first text event, cleaned up on result.
_stream_states: dict[str, _StreamState] = {}


async def _stream_text_to_channels(
    deps: OutputDeps,
    chat_jid: str,
    state: _StreamState,
    *,
    final: bool = False,
) -> None:
    """Push buffered text to channels that support update_message.

    On first call, posts a new message. Subsequent calls update it in-place.
    Throttled to _STREAM_THROTTLE unless ``final`` is True.

    Uses JID alias resolution so channels that don't own the canonical JID
    (e.g. Slack when the primary is a WhatsApp JID) can still stream.
    """
    now = time.monotonic()
    if not final and (now - state.last_update) < _STREAM_THROTTLE:
        return

    display = state.buffer + (" \u258c" if not final else "")
    state.last_update = now

    for ch in deps.channels:
        if not ch.is_connected():
            continue
        if not hasattr(ch, "update_message") or not hasattr(ch, "post_message"):
            continue

        # Resolve alias so e.g. Slack can stream using its slack:CHANNEL_ID JID
        target_jid = deps.get_channel_jid(chat_jid, ch.name) or chat_jid
        if not ch.owns_jid(target_jid):
            continue

        ch_name = getattr(ch, "name", "?")
        msg_id = state.message_ids.get(ch_name)

        try:
            if msg_id is None:
                msg_id = await ch.post_message(target_jid, display)
                if msg_id:
                    state.message_ids[ch_name] = msg_id
            else:
                await ch.update_message(target_jid, msg_id, display)
        except Exception as exc:
            logger.debug("Stream update failed", channel=ch_name, err=str(exc))


def _next_trace_id(prefix: str) -> str:
    """Generate a unique monotonic ID for trace DB rows.

    Uses itertools.count for thread-safe, atomic counter increments.
    """
    ts_ms = int(datetime.now(UTC).timestamp() * 1000)
    return f"{prefix}-{ts_ms}-{next(_trace_counter)}"


def _truncate_output(content: str) -> str:
    """Truncate long tool output for channel broadcast, keeping head and tail."""
    head = content[:2000]
    tail = content[-500:]
    omitted = len(content) - 2500
    return f"{head}\n\n... ({omitted} chars omitted) ...\n\n{tail}"


class OutputDeps(Protocol):
    """Dependencies for output handling."""

    @property
    def channels(self) -> list[Channel]: ...

    def get_channel_jid(self, canonical_jid: str, channel_name: str) -> str | None: ...

    async def broadcast_to_channels(
        self, chat_jid: str, text: str, *, suppress_errors: bool = True
    ) -> None: ...

    def emit(self, event: Any) -> None: ...


async def broadcast_trace(
    deps: OutputDeps,
    chat_jid: str,
    trace_type: str,
    data: dict[str, Any],
    channel_text: str,
    *,
    db_id_prefix: str,
    db_sender: str,
    message_type: str = "assistant",
) -> None:
    """Store a trace event, send to channels, and emit to EventBus."""
    ts = datetime.now(UTC).isoformat()
    await store_message_direct(
        id=_next_trace_id(db_id_prefix),
        chat_jid=chat_jid,
        sender=db_sender,
        sender_name=db_sender,
        content=json.dumps(data),
        timestamp=ts,
        is_from_me=True,
        message_type=message_type,
    )
    await deps.broadcast_to_channels(chat_jid, channel_text)
    deps.emit(AgentTraceEvent(chat_jid=chat_jid, trace_type=trace_type, data=data))


async def broadcast_agent_input(
    deps: OutputDeps,
    chat_jid: str,
    messages: list[dict],
    *,
    source: str = "user",
) -> None:
    """Broadcast agent input messages to channels so users see what the agent was told.

    For normal user messages (source="user"), only emits a trace event since
    users already see their own messages in chat. For synthetic messages
    (scheduled tasks, reset handoffs, IPC forwards), broadcasts the full
    prompt to channels so observers understand what triggered the agent.
    """
    _SOURCE_LABELS = {
        "scheduled_task": "Scheduled Task",
        "reset_handoff": "Context Handoff",
        "ipc_forward": "Forwarded",
    }

    if source == "user":
        # User messages are already visible in chat — just emit trace events
        # for TUI/SSE consumers who want the full token stream.
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            deps.emit(
                AgentTraceEvent(
                    chat_jid=chat_jid,
                    trace_type="user_input",
                    data={
                        "sender_name": msg.get("sender_name", "Unknown"),
                        "content": msg.get("content", ""),
                        "source": source,
                    },
                )
            )
        return

    # Synthetic messages: broadcast to channels so users see what triggered the agent
    label = _SOURCE_LABELS.get(source, source)
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", "")
        if len(content) > 500:
            content = content[:497] + "..."
        channel_text = f"\u00bb [{label}] {content}"
        await deps.broadcast_to_channels(chat_jid, channel_text)
        deps.emit(
            AgentTraceEvent(
                chat_jid=chat_jid,
                trace_type="agent_input",
                data={
                    "sender_name": msg.get("sender_name", "Unknown"),
                    "content": msg.get("content", ""),
                    "source": source,
                },
            )
        )


async def handle_streamed_output(
    deps: OutputDeps,
    chat_jid: str,
    group: RegisteredGroup,
    result: ContainerOutput,
) -> bool:
    """Handle a streamed output from the container agent.

    Broadcasts trace events and results to channels/TUI.
    Returns True if a user-visible result was sent.
    """
    from pynchy.messaging.router import strip_internal_tags

    s = get_settings()
    ts = datetime.now(UTC).isoformat()

    # --- Trace events: persist to DB + broadcast ---
    if result.type == "thinking":
        await broadcast_trace(
            deps,
            chat_jid,
            "thinking",
            {"thinking": result.thinking or ""},
            "\U0001f4ad thinking...",
            db_id_prefix="think",
            db_sender="thinking",
            message_type="assistant",
        )
        return False
    if result.type == "tool_use":
        tool_name = result.tool_name or "tool"
        tool_input = result.tool_input or {}
        _last_tool_name[chat_jid] = tool_name
        data = {"tool_name": tool_name, "tool_input": tool_input}
        preview = format_tool_preview(tool_name, tool_input)
        await broadcast_trace(
            deps,
            chat_jid,
            "tool_use",
            data,
            f"\U0001f527 {preview}",
            db_id_prefix="tool",
            db_sender="tool_use",
            message_type="assistant",
        )
        return False
    if result.type == "tool_result":
        content = result.tool_result_content or ""
        preceding_tool = _last_tool_name.pop(chat_jid, "")

        # For select tools, broadcast the result content instead of the
        # generic placeholder so users can review it (e.g. plan files).
        # Truncate if it exceeds the channel broadcast threshold.
        if preceding_tool in _VERBOSE_RESULT_TOOLS and content:
            display = _truncate_output(content) if len(content) > _MAX_TOOL_OUTPUT else content
            channel_text = f"\U0001f4cb {preceding_tool}:\n{display}"
        else:
            channel_text = "\U0001f4cb tool result"

        await broadcast_trace(
            deps,
            chat_jid,
            "tool_result",
            {
                "tool_use_id": result.tool_result_id or "",
                "content": content,
                "is_error": result.tool_result_is_error or False,
            },
            channel_text,
            db_id_prefix="toolr",
            db_sender="tool_result",
            message_type="assistant",
        )
        return False
    if result.type == "system":
        subtype = result.system_subtype or ""
        sys_data = result.system_data or {}
        data = {"subtype": subtype, "data": sys_data}

        # Build a descriptive log line per subtype
        if subtype == "init":
            sid = sys_data.get("session_id", "")
            sid_short = sid[:12] if sid else "none"
            channel_text = f"\u2699\ufe0f session {sid_short} (resumed)"
        else:
            channel_text = f"\u2699\ufe0f system: {subtype or 'unknown'}"

        # Always persist to DB and emit to EventBus
        ts = datetime.now(UTC).isoformat()
        await store_message_direct(
            id=_next_trace_id("sys"),
            chat_jid=chat_jid,
            sender="system",
            sender_name="system",
            content=json.dumps(data),
            timestamp=ts,
            is_from_me=True,
            message_type="system",
        )
        deps.emit(AgentTraceEvent(chat_jid=chat_jid, trace_type="system", data=data))

        # Suppress init from channels — it fires on every query and adds
        # no value for the user.  The descriptive text above is still
        # persisted to DB for debugging.
        if subtype != "init":
            await deps.broadcast_to_channels(chat_jid, channel_text)

        return False
    if result.type == "text":
        delta = result.text or ""
        deps.emit(
            AgentTraceEvent(
                chat_jid=chat_jid,
                trace_type="text",
                data={"text": delta},
            )
        )
        # Stream text deltas to channels that support update_message
        if delta:
            state = _stream_states.get(chat_jid)
            if state is None:
                state = _StreamState()
                _stream_states[chat_jid] = state
            state.buffer += delta
            await _stream_text_to_channels(deps, chat_jid, state)
        return False

    # Persist result metadata if present (cost, usage, duration)
    if result.result_metadata:
        meta = result.result_metadata
        await store_message_direct(
            id=_next_trace_id("meta"),
            chat_jid=chat_jid,
            sender="result_meta",
            sender_name="result_meta",
            content=json.dumps(meta),
            timestamp=ts,
            is_from_me=True,
            message_type="assistant",
        )
        cost = meta.get("total_cost_usd")
        duration = meta.get("duration_ms")
        turns = meta.get("num_turns")
        parts = []
        if cost is not None:
            parts.append(f"{cost:.2f} USD")
        if duration is not None:
            parts.append(f"{duration / 1000:.1f}s")
        if turns is not None:
            parts.append(f"{turns} turns")
        if parts:
            trace_text = f"\U0001f4ca {' \u00b7 '.join(parts)}"
            await deps.broadcast_to_channels(chat_jid, trace_text)
        deps.emit(
            AgentTraceEvent(
                chat_jid=chat_jid,
                trace_type="result_meta",
                data=meta,
            )
        )

    # Finalize any streaming state — update streamed messages with final text
    # or clean up if the result is empty.
    stream_state = _stream_states.pop(chat_jid, None)

    if result.result:
        raw = result.result if isinstance(result.result, str) else json.dumps(result.result)
        text = strip_internal_tags(raw)
        if text:
            is_host, content = parse_host_tag(text)
            if is_host:
                sender = "host"
                sender_name = "host"
                db_content = content
                channel_text = f"\U0001f3e0 {content}"
                logger.info("Host message", group=group.name, text=content[:200])
            else:
                sender = "bot"
                sender_name = s.agent.name
                db_content = text
                channel_text = f"🦞 {text}"
                logger.info("Agent output", group=group.name, text=raw[:200])
            msg_type = "host" if sender == "host" else "assistant"
            await store_message_direct(
                id=f"bot-{int(datetime.now(UTC).timestamp() * 1000)}",
                chat_jid=chat_jid,
                sender=sender,
                sender_name=sender_name,
                content=db_content,
                timestamp=ts,
                is_from_me=True,
                message_type=msg_type,
            )

            # For channels that were streaming, finalize the existing message.
            # For all others, post normally via broadcast.
            stream_ids = stream_state.message_ids if stream_state else None
            await finalize_stream_or_broadcast(
                deps, chat_jid, channel_text, stream_ids, suppress_errors=False
            )
            deps.emit(
                MessageEvent(
                    chat_jid=chat_jid,
                    sender_name=sender_name,
                    content=db_content,
                    timestamp=ts,
                    is_bot=True,
                )
            )
            return True

    return False
