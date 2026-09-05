"""Streamed output handling — processes container output and broadcasts to channels.

Dispatches container output events (thinking, tool_use, tool_result, system,
text, result) to appropriate handlers.  Channel streaming and trace batching
are delegated to ``_streaming``.

Extracted from app.py to keep the orchestrator focused on wiring.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pynchy.agent_protocol.api import (
    ContainerOutput,  # noqa: TC001 - beartype resolves router annotations at runtime.
)
from pynchy.conversation.api import new_turn_id
from pynchy.event_bus import AgentTraceEvent, MessageEvent
from pynchy.host.orchestrator.messaging.formatter import (
    format_internal_tags,
    format_tool_preview,
    parse_host_tag,
)
from pynchy.host.orchestrator.messaging.sender import broadcast
from pynchy.host.orchestrator.messaging.streaming import (
    OutputDeps,
    StreamState,
    TraceBatcher,
    close_trace_run,
    enqueue_tool_trace,
    finalize_active_stream,
    get_trace_batcher,
    init_trace_batcher,
    stream_states,
    stream_text_to_channels,
)
from pynchy.logger import logger
from pynchy.plugins.api import (  # beartype resolves router annotations at runtime.
    OutboundEvent,
    OutboundEventType,
)
from pynchy.state.api import mark_work_item_delivery_delivered_for_turn, store_message_direct
from pynchy.workspace.api import (
    WorkspaceProfile,  # noqa: TC001 - beartype resolves router annotations at runtime.
)

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


def _make_event(event_type: str, content: str, **metadata: object) -> OutboundEvent:
    """Construct an OutboundEvent with the given type, content, and metadata.

    The event_type string is resolved to an OutboundEventType enum value.
    Metadata kwargs are passed through for formatter-specific rendering hints.
    """
    type_map = {
        "thinking": OutboundEventType.THINKING,
        "tool_trace": OutboundEventType.TOOL_TRACE,
        "tool_result": OutboundEventType.TOOL_RESULT,
        "system": OutboundEventType.SYSTEM,
        "text": OutboundEventType.TEXT,
        "result": OutboundEventType.RESULT,
        "host": OutboundEventType.HOST,
    }
    return OutboundEvent(
        type=type_map.get(event_type, OutboundEventType.TEXT),
        content=content,
        metadata=dict(metadata) if metadata else {},
    )


# Per-chat outbound message IDs from the last final result.
# Populated by _handle_final_result(), consumed by pop_last_result_ids().
# Keyed by chat_jid -> {channel_name: raw_message_ts}.
_last_result_ids: dict[str, dict[str, str]] = {}

# Tool names whose tool_result content should be broadcast in full
# instead of the generic "📋 tool result" placeholder.
_VERBOSE_RESULT_TOOLS = frozenset({"ExitPlanMode", "EnterPlanMode"})
_CHANNEL_SUPPRESSED_SYSTEM_SUBTYPES = frozenset({"init", "thread.started"})

# Tracks the last tool_use name per chat so we can enrich the subsequent tool_result.
_last_tool_name: dict[str, str] = {}

# Channel broadcast truncation threshold for tool results.
# Full content is preserved in conversation storage; only the channel broadcast is truncated.
_MAX_TOOL_OUTPUT = 4000


@dataclass(frozen=True)
class _FinalResultRequest:
    deps: OutputDeps
    chat_jid: str
    group: WorkspaceProfile
    result: ContainerOutput
    ts: str
    stream_state: StreamState | None
    turn_id: str


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
) -> None:
    """Send a live trace event to channels and the EventBus."""
    event = _make_event("text", channel_text)
    if trace_type in {"tool_use", "tool_result"}:
        await enqueue_tool_trace(deps, chat_jid, event)
    else:
        await close_trace_run(chat_jid)
        await deps.broadcast_to_channels(chat_jid, event)
    deps.emit(AgentTraceEvent(chat_jid=chat_jid, trace_type=trace_type, data=data))


async def broadcast_agent_input(
    deps: OutputDeps,
    chat_jid: str,
    messages: list[dict[str, Any]],
    *,
    source: str = "user",
) -> None:
    """Broadcast agent input messages to channels so users see what the agent was told.

    For normal user messages (source="user"), only emits a trace event since
    users already see their own messages in chat. Hidden learning reviews emit
    nothing. Other synthetic messages (scheduled tasks, reset handoffs, IPC
    forwards) broadcast the full prompt to channels so observers understand
    what triggered the agent.
    """
    source_labels = {
        "scheduled_task": "Scheduled Task",
        "reset_handoff": "Context Handoff",
        "ipc_forward": "Forwarded",
    }

    if source in {
        "hidden_learning_review",
        "hidden_plan_review",
        "external:hidden_plan_review",
    }:
        return

    # An inbound prompt is an explicit boundary even when the channel already
    # displays the human's message and Pynchy emits only audit trace data.
    await close_trace_run(chat_jid)

    if source == "user":
        # User messages are already visible in chat. Emit trace events for
        # observers that need the complete agent-input record.
        for msg in messages:
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
    label = source_labels.get(source, source)
    if source.startswith("trusted:"):
        label = source.removeprefix("trusted:").title()
    for msg in messages:
        content = msg.get("content", "")
        if len(content) > 500:
            content = content[:497] + "..."
        channel_text = f"\u00bb [{label}] {content}"
        await deps.broadcast_to_channels(chat_jid, _make_event("text", channel_text))
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
    )


async def _handle_system(deps: OutputDeps, chat_jid: str, result: ContainerOutput) -> None:
    """Handle a system trace event.

    Emits to EventBus. Suppresses lifecycle events from channels since they fire
    on every query and add no value for the user.
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

    deps.emit(AgentTraceEvent(chat_jid=chat_jid, trace_type="system", data=data))

    # Suppress lifecycle events from channels — the descriptive text above is still
    # available to live EventBus consumers for debugging.
    if subtype not in _CHANNEL_SUPPRESSED_SYSTEM_SUBTYPES:
        await close_trace_run(chat_jid)
        await deps.broadcast_to_channels(chat_jid, _make_event("system", channel_text))


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
    # Stream text deltas to channels that support update_event
    if delta:
        state = stream_states.get(chat_jid)
        if state is None:
            # Starting a text stream — flush any pending traces first
            # so tool messages appear before this text in the channel.
            await close_trace_run(chat_jid)
            event = OutboundEvent(type=OutboundEventType.TEXT, content="")
            state = StreamState(event=event)
            stream_states[chat_jid] = state
        state.event.content += delta
        await stream_text_to_channels(deps, chat_jid, state)


async def _handle_result_metadata(deps: OutputDeps, chat_jid: str, meta: dict[str, Any]) -> None:
    """Broadcast result metadata summary and emit the live trace event."""
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
        await close_trace_run(chat_jid)
        await deps.broadcast_to_channels(chat_jid, _make_event("text", trace_text))
    deps.emit(
        AgentTraceEvent(
            chat_jid=chat_jid,
            trace_type="result_meta",
            data=meta,
        )
    )


async def _handle_final_result(request: _FinalResultRequest) -> tuple[bool, bool]:
    """Handle the final result event — store, broadcast, and emit.

    Returns whether a visible result existed and whether a channel received it.
    """
    if not request.result.result:
        return False, False

    raw = (
        request.result.result
        if isinstance(request.result.result, str)
        else json.dumps(request.result.result)
    )
    text = format_internal_tags(raw)
    if not text:
        return False, False

    is_host, content = parse_host_tag(text)
    if is_host:
        sender = "host"
        sender_name = "host"
        db_content = content
        event = _make_event("host", content)
        logger.info("Host message", group=request.group.name, text=content[:200])
    else:
        sender = "bot"
        sender_name = request.deps.agent_name
        db_content = text
        event = _make_event("result", text)
        logger.info("Agent output", group=request.group.name, text=raw[:200])

    msg_type = "host" if sender == "host" else "assistant"
    await store_message_direct(
        message_id=f"{request.turn_id}:{msg_type}:{request.ts}",
        chat_jid=request.chat_jid,
        sender=sender,
        sender_name=sender_name,
        content=db_content,
        timestamp=request.ts,
        is_from_me=True,
        message_type=msg_type,
        metadata={
            "source": "agent_result",
            "turn_id": request.turn_id,
            "workspace_name": request.group.name,
            "workspace_folder": request.group.folder,
        },
    )

    # For channels that were streaming, finalize the existing message.
    # For all others, post normally via broadcast.
    stream_ids = request.stream_state.message_ids if request.stream_state else None
    delivered = await broadcast(
        request.deps,
        request.chat_jid,
        event,
        suppress_errors=False,
        stream_message_ids=stream_ids,
        source="agent",
    )

    # Stash per-channel message IDs for post-run reactions (e.g. zzz).
    if stream_ids:
        _last_result_ids[request.chat_jid] = dict(stream_ids)

    request.deps.emit(
        MessageEvent(
            chat_jid=request.chat_jid,
            sender_name=sender_name,
            content=db_content,
            timestamp=request.ts,
            is_bot=True,
        )
    )
    return True, delivered


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
    *,
    turn_id: str | None = None,
) -> bool:
    """Handle a streamed output from the container agent.

    Dispatches to type-specific handlers for trace events (thinking,
    tool_use, tool_result, system, text) and final results.
    Returns True if a user-visible result was produced.
    """
    ts = datetime.now(UTC).isoformat()

    # --- Trace events: broadcast live; LiteLLM/Phoenix owns trace persistence ---
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
        await _handle_result_metadata(deps, chat_jid, result.result_metadata)

    # Finalize any streaming state — update streamed messages with final text
    # or clean up if the result is empty.
    stream_state = stream_states.pop(chat_jid, None)

    # A final result closes the tool message so a later run starts a separate one.
    await close_trace_run(chat_jid)

    resolved_turn_id = turn_id or new_turn_id()
    sent, delivered = await _handle_final_result(
        _FinalResultRequest(
            deps=deps,
            chat_jid=chat_jid,
            group=group,
            result=result,
            ts=ts,
            stream_state=stream_state,
            turn_id=resolved_turn_id,
        )
    )
    if delivered:
        try:
            await mark_work_item_delivery_delivered_for_turn(resolved_turn_id)
        except RuntimeError:
            logger.debug("Work-item delivery ledger unavailable after final result")
    return sent
