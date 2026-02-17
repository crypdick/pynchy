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

All service tool requests pass through a security policy middleware before reaching the plugin handler. Tools are assigned to risk tiers:

| Tier | Behavior | Example |
|------|----------|---------|
| `always-approve` | Executed without checks | Read-only queries |
| `rules-engine` | Deterministic rules (auto-approved for now) | Scoped operations |
| `human-approval` | Denied until a human approves via chat | Destructive operations |

God workspaces bypass all policy gates. Non-god workspaces fall back to strict defaults unless a security profile is configured. See [Security Model](security.md#5-mcp-service-tool-policy) for details.

## Handler Contract

Plugins implement the `pynchy_mcp_server_handler` hook and return a dict mapping tool names to async handler functions:

```python
@hookimpl
def pynchy_mcp_server_handler(self) -> dict[str, Any]:
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
| `sqlite-memory` | `save_memory`, `recall_memories`, `forget_memory`, `list_memories` | Per-group persistent memory |

For the full IPC protocol that carries service requests, see [IPC](ipc.md#service-requests).

---

**Want to customize this?** Write your own MCP service tool handler plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
