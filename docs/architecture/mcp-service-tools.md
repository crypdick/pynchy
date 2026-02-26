# MCP Service Tools

This page explains how host-side service tools work — tools that agents invoke via MCP but that execute on the host process rather than inside the container. Understanding this helps you build plugins that give agents access to host resources (calendars, databases, external APIs) while maintaining security boundaries.

MCP service tool handlers are pluggable. The built-in CalDAV plugin provides calendar tools, and the memory plugin provides memory tools. Additional host-side tools can be added via plugins.

## How Service Tools Work

Unlike regular MCP servers (which run inside the container), service tool handlers run on the **host process**. The agent calls an MCP tool normally, but the request travels through IPC to reach the host:

```
Agent → MCP tool call → IPC request → Host policy check → Plugin handler → IPC response → Agent
```

This architecture lets agents interact with host resources (calendars, databases, network services) that aren't accessible from inside the container sandbox.

## Security Policy

All service tool requests pass through `SecurityPolicy` before reaching the plugin handler. Each service declares four trust properties (`public_source`, `secret_data`, `public_sink`, `dangerous_writes`) that control how the policy gates access based on taint tracking.

Admin workspaces bypass all policy gates. Non-admin workspaces are gated by service trust declarations — see [Service Trust Policy](security.md#5-service-trust-policy-lethal-trifecta-defenses) for the architecture and [Service Trust](../usage/security.md) for configuration.

## Handler Contract

Plugins implement the `pynchy_service_handler` hook and return a dict mapping tool names to async handler functions:

```python
@hookimpl
def pynchy_service_handler(self) -> dict[str, Any]:
    return {
        "tools": {
            "list_calendar": _handle_list_calendar,
            "create_event": _handle_create_event,
        },
    }
```

Each handler receives the full IPC request dict and returns `{"result": ...}` on success or `{"error": "..."}` on failure.

## Built-in Handlers

| Plugin | Tools | Description |
|--------|-------|-------------|
| `caldav` | `list_calendars`, `list_calendar`, `create_event`, `delete_event` | CalDAV calendar access (Nextcloud, etc.) |
| `google-setup` | `setup_google_{profile}` | Idempotent Google setup — GCP project, API enablement, OAuth authorization. One tool per chrome profile. ([guide](../usage/gdrive.md)) |
| `sqlite-memory` | `save_memory`, `recall_memories`, `forget_memory`, `list_memories` | Per-group persistent memory |

For the full IPC protocol that carries service requests, see [IPC](ipc.md#service-requests).

---

**Want to customize this?** Write your own MCP service tool handler plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
