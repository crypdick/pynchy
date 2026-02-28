# Slack Blocks Formatter Design

**Date:** 2026-02-28
**Status:** Approved

## Problem

All outbound channel messages are rendered as plain text with emoji prefixes (🔧, 💭, 🦞, etc.). Slack receives the same flat text as every other channel, missing out on Block Kit's rich formatting: structured layouts, syntax-highlighted code, collapsible sections, and interactive elements.

The existing formatting code in `messaging/formatter.py` works well as a baseline for simple channels (WhatsApp, Telegram, etc.) but needs to be preserved as a reusable reference while Slack gets its own rich rendering path.

## Design

### Core change: event-based outbound pipeline

Replace the current `text: str` flowing through the pipeline with structured `OutboundEvent` objects. The pipeline handles orchestration (streaming, batching, finalization). Each channel owns rendering via a composable `BaseFormatter`.

### Architecture layers

```
Pipeline (router.py, streaming.py, sender.py)
    │
    │  produces OutboundEvent objects
    │  handles orchestration: streaming, batching, finalization
    │
    ▼
Channel.send_event(jid, event)
    │
    │  calls self.formatter.render(event) → RenderedMessage
    │  sends via channel's internal transport
    │
    ├── SlackChannel  →  SlackBlocksFormatter  →  blocks + text fallback
    ├── WhatsAppChannel  →  TextFormatter  →  plain text
    └── FutureChannel  →  TextFormatter (default) or custom
```

## Data types

### OutboundEvent (types.py)

```python
class OutboundEventType(Enum):
    TEXT = "text"                # Accumulated text (streaming or final)
    TOOL_TRACE = "tool_trace"   # Tool use preview
    TOOL_RESULT = "tool_result" # Tool output
    THINKING = "thinking"       # Extended thinking
    SYSTEM = "system"           # System events
    HOST = "host"               # Operational messages
    RESULT = "result"           # Final agent response

@dataclass
class OutboundEvent:
    type: OutboundEventType
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
```

Metadata varies by type:
- `TOOL_TRACE`: `{"tool_name": "Bash", "tool_input": {...}}`
- `TEXT`: `{"cursor": True, "streaming": True}`
- `RESULT`: `{"prefix_assistant_name": True}`

The `content` field always has a human-readable string. Formatters that don't understand a metadata key fall back to rendering `content` as plain text.

### RenderedMessage (messaging/formatters/base.py)

```python
@dataclass
class RenderedMessage:
    text: str                                   # Always present — universal fallback
    blocks: list[dict] | None = None            # Slack Block Kit (or similar structured payload)
    metadata: dict[str, Any] = field(default_factory=dict)
```

`text` is always required. Slack uses it for notifications and screen readers even when blocks are present.

## Formatter protocol

### BaseFormatter (messaging/formatters/base.py)

```python
class BaseFormatter(ABC):
    @abstractmethod
    def render(self, event: OutboundEvent) -> RenderedMessage: ...

    @abstractmethod
    def render_batch(self, events: list[OutboundEvent]) -> RenderedMessage: ...
```

### TextFormatter (messaging/formatters/text.py)

Refactored from the existing `formatter.py`. Captures the current rendering logic:
- `format_internal_tags()` for `<internal>` blocks → 🧠 _thought_
- `format_tool_preview()` for tool traces → 🔧 Bash: `cmd`
- Emoji prefixes per event type (🔧, 💭, 📋, ⚙️, 🦞, 🏠)
- `_truncate_output()` for long content (text channels lack native collapse)

`render_batch()` joins rendered text with `"\n"`.

Existing utility functions (`format_tool_preview`, `format_internal_tags`, `split_text`) remain as importable utilities that both TextFormatter and SlackBlocksFormatter use.

### SlackBlocksFormatter (plugins/channels/slack/_blocks.py)

Renders events to Slack Block Kit. Uses Slack's purpose-built block types:

| Event Type | Block Strategy |
|---|---|
| `TEXT` / `RESULT` | `markdown` block — Slack's LLM-specific block that natively renders standard markdown (code fences, tables, lists, headers). 12k char cumulative limit per payload. |
| `TOOL_TRACE` | `context` block (muted header: "🔧 Bash") + `rich_text_preformatted` (syntax-highlighted code via `language` property) |
| `THINKING` | `context` block — compact muted line: "💭 _thinking..._" |
| `TOOL_RESULT` | `context` block (header) + `rich_text_preformatted` (full monospace output, no truncation — Slack handles collapse) |
| `SYSTEM` / `HOST` | `context` block — small muted operational line |

`render_batch()` concatenates blocks from each event into one block list, respecting the 50-block-per-message budget.

No truncation on the Slack path — send full output and let Slack handle collapse via `expand: false` on section blocks and `rich_text_preformatted` for code.

## Channel protocol changes

```python
class Channel(Protocol):
    name: str
    formatter: BaseFormatter

    async def connect(self) -> None: ...
    async def send_event(self, jid: str, event: OutboundEvent) -> None: ...
    def is_connected(self) -> bool: ...
    def owns_jid(self, jid: str) -> bool: ...
    async def disconnect(self) -> None: ...
    async def reconnect(self) -> None: ...
    def prepare_shutdown(self) -> None: ...
    async def fetch_inbound_since(self, channel_jid: str, since: str) -> InboundFetchResult: ...

    # Optional streaming (checked with hasattr):
    #   post_event(jid, event) -> str | None     (returns message_id)
    #   update_event(jid, message_id, event)     (updates in-place)
```

`send_event` replaces `send_message` as THE protocol method for all outbound messages. No legacy fallback. Every channel implements `send_event`, calls `self.formatter.render(event)`, and sends via its internal transport.

`post_event` / `update_event` replace `post_message` / `update_message` for streaming channels.

## Pipeline changes

### router.py

Each handler produces `OutboundEvent` instead of formatted text. Example:

```python
# Before:
preview = format_tool_preview(tool_name, tool_input)
await enqueue_or_broadcast(deps, jid, f"🔧 {preview}")

# After:
event = OutboundEvent(
    type=OutboundEventType.TOOL_TRACE,
    content=output.content,
    metadata={"tool_name": tool_name, "tool_input": tool_input},
)
await enqueue_or_broadcast(deps, jid, event)
```

### streaming.py

`StreamState` holds a single mutable `OutboundEvent` whose `content` grows with each text delta:

```python
@dataclass
class StreamState:
    event: OutboundEvent          # TEXT event, content grows as deltas arrive
    message_ids: dict[str, str] = field(default_factory=dict)
    last_update: float = 0.0
```

When pushing to channels:
```python
state.event.metadata["cursor"] = not final
await ch.post_event(target_jid, state.event)  # or update_event
```

`TraceBatcher` collects `OutboundEvent` objects, calls `ch.send_event()` on flush.

### sender.py

Core knows nothing about blocks or rendering. Calls `ch.send_event(jid, event)` and lets the channel handle it:

```python
async def broadcast(deps, chat_jid, event: OutboundEvent, ...):
    targets = _resolve_send_targets(deps, chat_jid)
    for ch, target_jid in targets:
        await ch.send_event(target_jid, event)
```

Ledger records `event.content` (the text field) for reconciliation.

## Interactive features

### Approval buttons (replacing text commands)

Current: user types `approve a1` / `deny a1` in chat.
After: `context_actions` block with ✅ Approve / ❌ Deny buttons inline after the approval notification.

- Button `action_id` encodes the approval short ID (e.g., `cop_approve_a1`, `cop_deny_a1`)
- Interaction callback routes to existing `process_approval_decision()` — same backend
- After click: update message to remove buttons, add "✅ Approved by @user" context line
- Text command path stays functional for non-Slack channels

### Stop button during agent execution

"Stop" button appended to streaming messages while the agent is running.

- `action_id`: `agent_stop_{group_name}`
- Interaction callback signals cancellation to the agent execution loop
- On finalize: stop button removed via update_event
- On click: update message to "⏹ Stopped by @user"

### Improved ask_user

- Single-select: radio button group (cleaner than N separate buttons)
- Multi-select (`multiSelect: true`): checkbox group with Submit
- Free-text input: stays as-is (`plain_text_input`)
- Falls back to button layout for > 4 options

### Full output with native collapse

- `rich_text_preformatted` for tool output — no documented char limit, monospace, syntax highlighting
- Section block `expand: false` for long prose/markdown
- No truncation on the Slack path — send full output, let Slack collapse
- `TextFormatter` keeps `_truncate_output()` for text-only channels

## File layout

```
src/pynchy/
├── types.py                                    # + OutboundEvent, OutboundEventType
├── host/orchestrator/messaging/
│   ├── formatters/
│   │   ├── __init__.py                         # re-exports BaseFormatter, TextFormatter
│   │   ├── base.py                             # BaseFormatter ABC, RenderedMessage
│   │   └── text.py                             # TextFormatter (refactored from formatter.py)
│   ├── formatter.py                            # utility functions (format_tool_preview, etc.)
│   ├── router.py                               # produces OutboundEvent instead of text
│   ├── streaming.py                            # StreamState holds OutboundEvent
│   └── sender.py                               # calls ch.send_event(), no rendering logic
└── plugins/channels/slack/
    ├── _blocks.py                              # SlackBlocksFormatter (new)
    ├── _ui.py                                  # ask_user blocks (existing, enhanced)
    ├── _channel.py                             # send_event, post_event, update_event, interaction handlers
    └── __init__.py                             # plugin hook
```

`formatter.py` stays as a utility module — `format_tool_preview()`, `format_internal_tags()`, `split_text()` remain importable by both TextFormatter and SlackBlocksFormatter.

## What stays the same

- Streaming throttle timing (0.5s)
- TraceBatcher debounce mechanics (3s cooldown)
- `_resolve_send_targets()` channel filtering
- Ledger recording (best-effort, records text)
- Channel connection/reconnect/disconnect lifecycle
- Inbound message handling
- Existing `_ui.py` ask_user blocks (enhanced, not replaced)
- Slack Socket Mode transport
