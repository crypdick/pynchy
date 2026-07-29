# Observers

The event observation system — how Pynchy emits events and how plugins subscribe to persist or process them. Use this page to build monitoring, analytics, or debugging tools for your Pynchy installation.

Observers are pluggable. The built-in observer stores operational events to
SQLite, including a bounded evidence projection for live agent traces. The
LiteLLM gateway exports metadata-only LLM traces to Phoenix; prompt and response
content is disabled at the generated proxy configuration boundary.

## Event Bus

Pynchy uses a small asyncio event dispatcher. Components emit events during normal operation, and observers subscribe to the event types they care about.

**Design properties:**

- **Fire-and-forget** — emission is non-blocking (creates async tasks)
- **Type-based subscription** — listeners subscribe to specific event types, not all events
- **Error isolation** — listener exceptions are logged but don't propagate to the emitter

## Event Types

| Event | Fields | Emitted when |
|-------|--------|-------------|
| `MessageEvent` | `chat_jid`, `sender_name`, `content`, `timestamp`, `is_bot` | A message is stored (inbound or outbound) |
| `AgentActivityEvent` | `chat_jid`, `active` | An agent starts or stops processing |
| `AgentTraceEvent` | `chat_jid`, `trace_type`, `data` | Agent emits a trace (thinking, tool use, intermediate text) |
| `ChatClearedEvent` | `chat_jid` | Chat history is cleared |

Events are emitted from the message pipeline (`session_handler`, `message_handler`, `output_handler`).

## Observer Contract

Plugins implement the `pynchy_observer` hook and return an object with:

| Attribute / Method | Type | Description |
|--------------------|------|-------------|
| `name` | `str` | Observer identifier (e.g., `"sqlite"`, `"otel"`) |
| `subscribe(event_bus)` | `(EventBus) → None` | Attach listeners to the event bus |
| `close()` | `async () → None` | Async teardown — unsubscribe and flush |

Multiple observers can coexist — each subscribes independently to the event bus during startup and is closed gracefully during shutdown.

## Built-in: sqlite-observer

Persists operational events to a dedicated `events` table in the main SQLite database.

**What it stores:** message summaries, agent activity, and chat-clear events.
Message content is truncated to 500 characters. For `AgentTraceEvent`, SQLite
stores tool names, bounded tool inputs, bounded tool results, and bounded text.
The observer removes control characters, replaces detected credentials and
personal identifiers with irreversible redaction markers, limits collection
depth and size, and omits payload bodies for thinking, system, and input trace
types. The security Cop reads only the projected tool names, not tool inputs or
results.

Use this SQLite projection for a bounded operational evidence packet. Use
Phoenix for token, cost, timing, model, and provider-request metadata without
prompt or response bodies.

**Indexes:** event type, chat JID, and timestamp — for querying event history by group or time range.

---

**Want to customize this?** Write your own observer plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
