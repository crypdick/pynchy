# Connection runtime hook

## `pynchy_connection_runtime`

Provide one or more named, host-side runtimes for authenticated external
provider identities:

```python
@hookimpl
def pynchy_connection_runtime(self) -> tuple[ConnectionRuntime, ...]:
    return (MyConnectionRuntime("personal-account"),)
```

A `ConnectionRuntime` implements:

```python
name: str

async def start(self, context: ConnectionRuntimeContext) -> None: ...
async def close(self) -> None: ...
def is_ready(self) -> bool: ...
```

Runtime names must be unique across all plugins. Pynchy rejects malformed
contributions and duplicate names at startup. It starts runtimes only after the
database has released orphan delivery claims while preserving claims owned by
surviving turns, and after the message queue can accept work. Runtimes are ready
before interrupted turns are dispatched. If one runtime fails to start, Pynchy
closes that runtime and every runtime started before it in reverse order, then
fails startup and invokes deploy rollback when applicable.

`ConnectionRuntimeContext` exposes host-owned callbacks for channels,
workspaces, workspace registration, session binding, and inbound message
ingestion. A runtime should durably authenticate and deduplicate provider input
before calling `ingest_message`. `close()` must stop background work and remove
ephemeral runtime bindings; durable receipts and cursors should survive a
restart. `is_ready()` must reflect current provider-loop health rather than only
whether `start()` returned once.

Connection runtimes are not channels. They do not receive ordinary agent output
and should not implement destination selection through the general message
router. If a connection also exposes agent tools, register route-scoped typed
host actions through [`pynchy_service_handler`](host-services.md#pynchy_service_handler).

The built-in Matrix integration is the reference implementation. It returns one
runtime for each named Matrix connection that has at least one enabled route.
See [Matrix communications](../../integrations/matrix-gateway.md).

To package, register, and validate a connection plugin, follow the [plugin
authoring guide](../index.md).
