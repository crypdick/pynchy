# Observers

This page explains the event observation system — how Pynchy emits events and how plugins can subscribe to persist or process them. Understanding this helps you build monitoring, analytics, or debugging tools for your Pynchy installation.

Observers are pluggable. The built-in observer stores events to SQLite, but alternative backends (OpenTelemetry, log files, external services) can be added via plugins.

## Event Bus

Pynchy uses a lightweight asyncio event dispatcher. Components emit events during normal operation, and observers subscribe to the event types they care about.

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

Persists all events to a dedicated `events` table in the main SQLite database.

**What it stores:** event type, chat JID, timestamp, and a JSON payload with event-specific fields. Message content is truncated to 500 characters.

**Indexes:** event type, chat JID, and timestamp — designed for querying event history by group or time range.

---

**Want to customize this?** Write your own observer plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
