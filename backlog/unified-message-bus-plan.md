# Unified Message Bus — Implementation Plan

## Status: Done

## Problem

The system has multiple code paths that manually iterate over channels with slightly different formatting, error handling, and JID resolution logic. This duplication caused the bugs fixed in commit `5523e1cd`:

1. `session_handler.ingest_user_message()` — cross-channel user message echo (had sender-name prefixing)
2. `channel_handler.broadcast_to_channels()` — generic outbound broadcast (used by host messages, traces, agent output)
3. `message_handler.start_message_loop()` — formats `sender_name: content` for container IPC stdin
4. `output_handler.handle_streamed_output()` — formats `agent_name: text` for outbound agent results, with streaming support

Each of these manually loops over `deps.channels`, resolves JIDs via `deps.get_channel_jid()`, and handles errors differently. When a new channel is added or behavior needs to change, all 4 paths must be updated in sync.

## What Was Already Fixed (5523e1cd)

- **Sender-name prefix removed** from cross-channel broadcast in `session_handler.py` — now sends raw `msg.content`
- **Slack trigger detection** — `_strip_bot_mention` renamed to `_normalize_bot_mention`, replaces `<@BOTID>` with canonical trigger `@AgentName` instead of stripping it
- **Shutdown notification** added to `app.py._shutdown()` — broadcasts to god group before teardown

## What Remains: Unify the Broadcast Paths

### Goal
All outbound message routing should go through a single message bus that handles:
- Channel iteration with JID aliasing
- Error handling (suppress vs raise)
- Message type awareness (user echo, bot output, host notification, trace)
- Streaming support (for channels that support `update_message`)

### Proposed Architecture

#### 1. Create `src/pynchy/messaging/bus.py`

A `MessageBus` class (or module-level functions) that replaces the scattered broadcast logic:

```python
@dataclass
class OutboundMessage:
    chat_jid: str
    content: str
    kind: Literal["user_echo", "bot_output", "host", "trace", "ipc_forward"]
    sender_name: str | None = None  # For attribution where needed
    suppress_errors: bool = True

async def broadcast(deps, msg: OutboundMessage) -> None:
    """Single broadcast path for ALL outbound messages."""
    for ch in deps.channels:
        if not ch.is_connected():
            continue
        target_jid = deps.get_channel_jid(msg.chat_jid, ch.name) or msg.chat_jid
        # Format per message kind + channel capabilities
        text = _format_for_channel(ch, msg)
        try:
            await ch.send_message(target_jid, text)
        except (OSError, TimeoutError, ConnectionError) as exc:
            if not msg.suppress_errors:
                raise
            logger.warning("Channel send failed", channel=ch.name, err=str(exc))
```

#### 2. Formatting per message kind

- `user_echo`: Raw content only (channels show authorship natively). Skip source channel.
- `bot_output`: Prefix with agent name IF `channel.prefix_assistant_name` is not False
- `host`: Prefix with 🏠 emoji
- `trace`: Prefix with appropriate emoji (🔧, 💭, 📋, etc.)
- `ipc_forward`: Format with `» [Forwarded]` prefix

#### 3. Streaming support

The bus should handle streaming for channels that support `post_message` + `update_message`. The current `_StreamState` logic in `output_handler.py` can move into the bus.

#### 4. Migration path

1. Create `bus.py` with the unified broadcast function
2. Update `channel_handler.broadcast_to_channels()` to delegate to bus
3. Update `channel_handler.broadcast_host_message()` to delegate to bus
4. Update `session_handler.ingest_user_message()` to use bus for cross-channel echo
5. Update `output_handler.handle_streamed_output()` to use bus for bot output
6. Remove duplicated channel iteration from each caller
7. Tests: verify all message kinds route through the bus with correct formatting

### Files to Modify

| File | Change |
|------|--------|
| `src/pynchy/messaging/bus.py` | NEW — unified broadcast logic |
| `src/pynchy/messaging/channel_handler.py` | Delegate to bus |
| `src/pynchy/session_handler.py` | Use bus for cross-channel echo |
| `src/pynchy/messaging/output_handler.py` | Use bus for bot output + streaming |
| `src/pynchy/messaging/message_handler.py` | No change needed (IPC stdin formatting is distinct from channel broadcast) |

### Risk Assessment

- **Low risk**: The bus is a refactor of existing working code, not new behavior
- **Key invariant**: The IPC stdin path (`message_handler.py` lines 481-483) is intentionally different from channel broadcast — it formats `sender_name: content` for the container's multi-turn conversation. This should NOT be unified with channel broadcast.
- **Streaming**: The trickiest part is migrating the `_StreamState` logic without breaking Slack's in-place message updates
