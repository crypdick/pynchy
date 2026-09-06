# MCP Service Tools

How host-side service tools work — tools that agents invoke via MCP but that run on the host process rather than inside the container. Use this page to build plugins that give agents access to host resources (calendars, databases, external APIs) while keeping security boundaries intact.

MCP service tool handlers are pluggable. Built-in handlers provide calendar,
Google setup, and Gog tools. Additional host-side tools can be added via
plugins.

## How Service Tools Work

Unlike regular MCP servers (which Pynchy manages as Docker containers, host
subprocesses, or remote URLs), service tool handlers run in the **host
process**. The agent calls an MCP tool normally, but the request travels
through IPC to reach the host:

```
Agent → MCP tool call → IPC request → Host policy check → Plugin handler → IPC response → Agent
```

This lets agents interact with host resources (calendars, databases, network services) that aren't reachable from inside the container sandbox.

## Security Policy

All service tool requests pass through `SecurityGate` before reaching the plugin handler. Each service declares four trust properties (`public_source`, `secret_data`, `public_sink`, `dangerous_writes`) that control gating based on taint tracking.

All workspaces are gated by tool trust declarations. Admin workspaces are additionally protected by the clean-room rule that forbids public-source tools — see [Service Trust Policy](security.md#5-service-trust-policy-lethal-trifecta-defenses) for the architecture and [Tool Trust](../usage/security.md) for configuration.

## Handler Contract

Plugins implement `pynchy_service_handler` and return immutable typed
descriptors. The descriptor joins the handler to its semantic actions,
read/write classification, approval expiry, idempotency rule, audit contract,
requirements, safe availability probe, and operator guidance:

```python
@hookimpl
def pynchy_service_handler(self) -> HostActionRegistration:
    return CALENDAR_HOST_ACTIONS

@hookimpl
def pynchy_action_specs(self) -> tuple[ActionSpec, ...]:
    return CALENDAR_ACTION_SPECS
```

Each handler receives the full IPC request dict and returns `{"result": ...}` on success or `{"error": "..."}` on failure.

At startup, Pynchy composes built-in and plugin `ActionSpec` values, validates
typed host-action registrations, and rejects unknown actions, duplicate
capability or tool IDs, and write actions without idempotency or terminal audit contracts.
The existing `SecurityGate` remains the authority: `/status` and
`/capabilities` are diagnostic snapshots, while every dispatch and approved
replay checks current policy again.

The built-in MCP proxy advertises a stable tool schema, so the host treats the
resolved workspace profile as authoritative at dispatch. It rejects an omitted
workspace-tool requirement before policy can create a human approval. Approved
replay performs the same check, which prevents an old approval from restoring a
capability removed from the workspace profile.

The hook registers the host half of a tool. Its in-container IPC proxy must be
present in the selected agent image; host plugins are not imported into an
already-running agent container. See the [host-service hook reference](../plugins/hooks/host-services.md#pynchy_service_handler)
for the full descriptor shape.

## Built-in Handlers

| Plugin | Tools | Description |
|--------|-------|-------------|
| `caldav` | `list_calendars`, `list_calendar`, `create_event`, `delete_event` | CalDAV calendar access (Nextcloud, etc.) |
| `google-setup` | `setup_google_{profile}` | Idempotent Google setup — GCP project, API enablement, OAuth authorization. One tool per chrome profile. ([guide](../integrations/google/index.md)) |
| `gog` | `gog_*` | Reviewed host-only Gmail, Contacts, Docs, and Sheets operations. ([guide](../integrations/google/workspace-gog.md)) |

For the full IPC protocol that carries service requests, see [IPC](ipc.md#service-requests).

---

**Want to customize this?** Write your own MCP service tool handler plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
