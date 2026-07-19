# Observer and memory hooks

## `pynchy_observer`

Provide an event observer that subscribes to Pynchy's EventBus:

```python
@hookimpl
def pynchy_observer(self) -> EventObserver | None:
    return SqliteEventObserver()
```

Pynchy calls `subscribe(event_bus)` for every observer at startup. An observer
exposes `name`, `subscribe(event_bus)`, and `async close()`. It can receive
message, agent-activity, agent-trace, and chat-cleared events. Keep handlers
light and non-blocking: observers run in the host process and can delay event
dispatch.

## `pynchy_memory`

Provide persistent memory tools:

```python
@hookimpl
def pynchy_memory(self) -> MemoryBackend | None:
    return MyMemoryBackend()
```

Pynchy selects the first non-`None` backend. Implement `save`, `recall`,
`forget`, `list_keys`, `init`, and `close`. Memory stays isolated by workspace
folder. The built-in SQLite backend stores FTS5-indexed data in
`data/memories.db`; see [Memory](../../usage/memory.md).
