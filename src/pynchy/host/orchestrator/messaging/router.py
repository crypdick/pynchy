"""Streamed output handling — processes container output and broadcasts to channels.

Dispatches container output events (thinking, tool_use, tool_result, system,
text, result) to appropriate handlers.  Channel streaming and trace batching
are delegated to ``_streaming``.

Extracted from app.py to keep the orchestrator focused on wiring.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from itertools import count
from typing import TYPE_CHECKING, Any

from pynchy.config import get_settings
from pynchy.event_bus import AgentTraceEvent, MessageEvent
from pynchy.host.orchestrator.messaging.formatter import format_tool_preview, parse_host_tag
from pynchy.host.orchestrator.messaging.sender import finalize_stream_or_broadcast
from pynchy.host.orchestrator.messaging.streaming import (
    OutputDeps,
    StreamState,
    TraceBatcher,
    enqueue_or_broadcast,
    finalize_active_stream,
    get_trace_batcher,
    init_trace_batcher,
    stream_states,
    stream_text_to_channels,
)
from pynchy.logger import logger
from pynchy.state import store_message_direct
from pynchy.utils import generate_message_id

if TYPE_CHECKING:
    from pynchy.types import ContainerOutput, WorkspaceProfile

# Re-export for consumers that import from this module (app.py uses these)
__all__ = [
    "OutputDeps",
    "TraceBatcher",
    "_last_result_ids",
    "broadcast_agent_input",
    "broadcast_trace",
    "get_trace_batcher",
    "handle_streamed_output",
    "init_trace_batcher",
    "pop_last_result_ids",
]

_trace_counter = count(1)

# Per-chat outbound message IDs from the last final result.
# Populated by _handle_final_result(), consumed by pop_last_result_ids().
# Keyed by chat_jid -> {channel_name: raw_message_ts}.
_last_result_ids: dict[str, dict[str, str]] = {}

# Tool names whose tool_result content should be broadcast in full
# instead of the generic "📋 tool result" placeholder.
_VERBOSE_RESULT_TOOLS = frozenset({"ExitPlanMode", "EnterPlanMode"})

# Tracks the last tool_use name per chat so we can enrich the subsequent tool_result.
_last_tool_name: dict[str, str] = {}

# Channel broadcast truncation threshold for tool results.
# Full content is always persisted to DB; only the channel broadcast is truncated.
_MAX_TOOL_OUTPUT = 4000


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
    await enqueue_or_broadcast(deps, chat_jid, channel_text)
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


async def _handle_thinking(deps: OutputDeps, chat_jid: str, result: ContainerOutput) -> None:
    """Handle a thinking trace event."""
    # Finalize any in-progress text stream so it becomes its own message
    # before the thinking trace appears.
    await finalize_active_stream(deps, chat_jid)

    thinking = result.thinking or ""
    if thinking:
        display = _truncate_output(thinking) if len(thinking) > _MAX_TOOL_OUTPUT else thinking
        channel_text = f"\U0001f4ad {display}"
    else:
        channel_text = "\U0001f4ad thinking..."

    await broadcast_trace(
        deps,
        chat_jid,
        "thinking",
        {"thinking": thinking},
        channel_text,
        db_id_prefix="think",
        db_sender="thinking",
        message_type="assistant",
    )


async def _handle_tool_use(deps: OutputDeps, chat_jid: str, result: ContainerOutput) -> None:
    """Handle a tool_use trace event."""
    # Finalize any in-progress text stream so text before this tool call
    # becomes its own message, preserving chronological interleaving.
    await finalize_active_stream(deps, chat_jid)

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


async def _handle_tool_result(deps: OutputDeps, chat_jid: str, result: ContainerOutput) -> None:
    """Handle a tool_result trace event."""
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


async def _handle_system(deps: OutputDeps, chat_jid: str, result: ContainerOutput) -> None:
    """Handle a system trace event.

    Persists to DB and emits to EventBus. Suppresses init events from
    channels since they fire on every query and add no value for the user.
    """
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

    # Suppress init from channels — the descriptive text above is still
    # persisted to DB for debugging.
    if subtype != "init":
        await enqueue_or_broadcast(deps, chat_jid, channel_text)


async def _handle_text(deps: OutputDeps, chat_jid: str, result: ContainerOutput) -> None:
    """Handle a text delta event — accumulates into streaming state."""
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
        state = stream_states.get(chat_jid)
        if state is None:
            # Starting a new text stream — flush any pending traces first
            # so tool messages appear before this text in the channel.
            batcher = get_trace_batcher()
            if batcher is not None:
                await batcher.flush(chat_jid)
            state = StreamState()
            stream_states[chat_jid] = state
        state.buffer += delta
        await stream_text_to_channels(deps, chat_jid, state)


async def _handle_result_metadata(
    deps: OutputDeps, chat_jid: str, meta: dict[str, Any], ts: str
) -> None:
    """Persist result metadata (cost, usage, duration) and broadcast summary."""
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
        await enqueue_or_broadcast(deps, chat_jid, trace_text)
    deps.emit(
        AgentTraceEvent(
            chat_jid=chat_jid,
            trace_type="result_meta",
            data=meta,
        )
    )


async def _handle_final_result(
    deps: OutputDeps,
    chat_jid: str,
    group: WorkspaceProfile,
    result: ContainerOutput,
    ts: str,
    stream_state: StreamState | None,
) -> bool:
    """Handle the final result event — store, broadcast, and emit.

    Returns True if a user-visible result was sent.
    """
    from pynchy.host.orchestrator.messaging.formatter import strip_internal_tags

    if not result.result:
        return False

    raw = result.result if isinstance(result.result, str) else json.dumps(result.result)
    text = strip_internal_tags(raw)
    if not text:
        return False

    s = get_settings()
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
        id=generate_message_id("bot"),
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

    # Stash per-channel message IDs for post-run reactions (e.g. zzz).
    if stream_ids:
        _last_result_ids[chat_jid] = dict(stream_ids)

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


def pop_last_result_ids(chat_jid: str) -> dict[str, str] | None:
    """Pop and return per-channel outbound message IDs for the last result.

    Returns None if no IDs were stashed (no text result was sent).
    """
    return _last_result_ids.pop(chat_jid, None)


async def handle_streamed_output(
    deps: OutputDeps,
    chat_jid: str,
    group: WorkspaceProfile,
    result: ContainerOutput,
) -> bool:
    """Handle a streamed output from the container agent.

    Dispatches to type-specific handlers for trace events (thinking,
    tool_use, tool_result, system, text) and final results.
    Returns True if a user-visible result was sent.
    """
    ts = datetime.now(UTC).isoformat()

    # --- Trace events: persist to DB + broadcast ---
    if result.type == "thinking":
        await _handle_thinking(deps, chat_jid, result)
        return False
    if result.type == "tool_use":
        await _handle_tool_use(deps, chat_jid, result)
        return False
    if result.type == "tool_result":
        await _handle_tool_result(deps, chat_jid, result)
        return False
    if result.type == "system":
        await _handle_system(deps, chat_jid, result)
        return False
    if result.type == "text":
        await _handle_text(deps, chat_jid, result)
        return False

    # --- Final result: metadata + result text ---
    if result.result_metadata:
        await _handle_result_metadata(deps, chat_jid, result.result_metadata, ts)

    # Finalize any streaming state — update streamed messages with final text
    # or clean up if the result is empty.
    stream_state = stream_states.pop(chat_jid, None)

    # Flush any buffered traces before the bot reply so ordering is preserved.
    batcher = get_trace_batcher()
    if batcher is not None:
        await batcher.flush(chat_jid)

    return await _handle_final_result(deps, chat_jid, group, result, ts, stream_state)
