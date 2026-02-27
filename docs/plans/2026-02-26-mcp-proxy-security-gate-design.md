# MCP Proxy & Security Gate Design

**Date:** 2026-02-26
**Status:** Implemented
**Supersedes:** Portions of the Playwright browser plugin design (security middleware section)

## Summary

Add a centralized MCP proxy to McpManager that routes all MCP traffic through the existing lethal trifecta defense (`SecurityPolicy`). Today, MCP tool calls bypass security enforcement entirely — agents connect directly to MCP servers via `host.docker.internal`. This design closes that gap and provides the security middleware needed by the Playwright browser plugin (and all other MCP servers).

## Problem

Security enforcement has a transport-shaped hole:

| Transport | Path | Security gated? |
|-----------|------|-----------------|
| IPC service tools | Container → IPC → `_handlers_service.py` → plugin handler | Yes |
| MCP tools | Container → **direct to MCP server** | **No** |

An agent using MCP tools (Slack, Google Drive, notebook, browser) can read untrusted content, access secrets, and write to external channels without any taint tracking, Cop inspection, or human approval.

Additionally, `_handlers_service.py` creates a fresh `SecurityPolicy` per request, so taint doesn't accumulate across calls within a session. This is a bug — taint should be sticky per container invocation.

## Goals

- All MCP traffic routes through a shared security enforcement point
- Lethal trifecta defense applies uniformly to IPC and MCP
- Taint tracking is session-scoped (sticky per container invocation)
- Single enforcement codepath — no parallel implementations
- Playwright browser plugin works with no custom security code
- Trust config lives alongside MCP config (unified, not separate namespaces)

## Non-Goals

- Replacing LiteLLM for LLM traffic (gateway stays as-is)
- Outbound MCP request inspection (future consideration)
- Multi-format browser snapshots (playwright-mcp's format is sufficient)

## Architecture

```
Agent Container (Claude Code)
  │
  │ MCP protocol (HTTP)
  │
  ▼
┌─────────────────────────────────────────────────────────┐
│  MCP Proxy (aiohttp, single port in McpManager)         │
│                                                          │
│  Route: POST /mcp/<group_folder>/<invocation_ts>/<iid>   │
│                                                          │
│  ┌────────────────────────────────┐                      │
│  │ SecurityGate (session-scoped)  │                      │
│  │  • SecurityPolicy.evaluate_*() │                      │
│  │  • Taint tracking (sticky)     │                      │
│  │  • Audit logging               │                      │
│  │  • Cop gate                    │                      │
│  │  • Human approval              │                      │
│  └──────────────┬─────────────────┘                      │
│                 │                                         │
│  ┌──────────────▼─────────────────┐                      │
│  │ Untrusted content fencing      │  For public_source   │
│  │ (response middleware)          │  servers only         │
│  └──────────────┬─────────────────┘                      │
│                 │                                         │
└─────────────────┼─────────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────┐
│  MCP Backend (Docker container, host script, or URL)     │
│  e.g., playwright-mcp, notebook, gdrive, slack           │
└─────────────────────────────────────────────────────────┘
```

The same SecurityGate instance is shared by the MCP proxy and the IPC handler for a given session.

## Component Design

### 1. SecurityGate (`security/gate.py`)

Session-scoped security enforcement. One instance per container invocation. Shared by IPC and MCP callers.

```python
class SecurityGate:
    """Session-scoped security enforcement for all tool calls."""

    def __init__(self, source_group: str, is_admin: bool, deps: IpcDeps):
        self._policy = SecurityPolicy(
            _resolve_security(source_group, is_admin=is_admin)
        )
        # ... audit, cop, approval coordination

    async def evaluate(
        self, tool_name: str, data: dict, request_id: str,
        *, direction: Literal["read", "write"] = "write",
    ) -> GateResult:
        """Single entry point for all security decisions.

        Handles: policy eval → audit → cop gate → human approval.
        Returns GateResult: allowed / denied / pending_approval.
        """

# Registry keyed by (group_folder, invocation_ts) for future-proofing
# against concurrent containers for the same group.
_gates: dict[tuple[str, float], SecurityGate] = {}

def create_gate(source_group: str, invocation_ts: float, ...) -> SecurityGate: ...
def get_gate(source_group: str, invocation_ts: float) -> SecurityGate | None: ...
def destroy_gate(source_group: str, invocation_ts: float) -> None: ...
```

**Lifecycle:** `create_gate()` called from `_orchestrator.py` at container spawn. `destroy_gate()` called when the container exits. The `invocation_ts` is a monotonic timestamp generated at spawn time.

**Taint bug fix:** `_handlers_service.py` stops creating per-request `SecurityPolicy` and instead calls `get_gate()` to use the session-scoped instance.

### 2. MCP Proxy (`container_runner/_mcp_proxy.py`)

Lightweight aiohttp server managed by McpManager.

- **Single port**, path-based routing: `POST /mcp/<group_folder>/<invocation_ts>/<instance_id>`
- Extracts `(group_folder, invocation_ts)` to look up the SecurityGate
- Extracts `instance_id` to find the backend URL
- Calls `gate.evaluate()` before forwarding
- Applies untrusted content fencing to responses from `public_source=true` servers
- Returns MCP-protocol-compliant errors on denial

**Fencing** (adapted from OpenClaw's `external-content.ts`):
1. Marker sanitization — scan for spoofed boundary markers (including Unicode homoglyphs)
2. Random-ID fences — wrap with `<<<EXTERNAL_UNTRUSTED_CONTENT id="{random}">>>` markers
3. Security warning — prepend notice telling the LLM not to treat content as instructions

Applied only to responses from servers with `public_source=true`.

**Cop integration:** For `public_source=true` servers, browser snapshots are routed through `cop.inspect_inbound()`. Flagged content is dropped entirely; the agent receives a generic error. Full details go to audit log.

**McpManager changes:**
- `sync()` starts the proxy
- `stop_all()` stops it
- `get_direct_server_configs()` returns proxy URLs instead of direct backend URLs

### 3. Playwright Browser Plugin (`integrations/plugins/playwright_browser.py`)

Single-file plugin. Named `browser` (not `playwright`) for user-friendliness.

**`pynchy_mcp_server_spec()`** — registers playwright-mcp with trust defaults:

```python
{
    "name": "browser",
    "type": "script",
    "command": "npx",
    "args": ["@anthropic-ai/playwright-mcp", "--config", config_path],
    "port": 9100,
    "transport": "streamable_http",
    "idle_timeout": 300,
    "trust": {
        "public_source": True,
        "secret_data": False,
        "public_sink": False,
        "dangerous_writes": False,
    },
}
```

Config file generated dynamically per-workspace from config.toml settings (capabilities, allowed_origins, headless, etc.).

**`pynchy_skill_paths()`** — contributes `container/skills/browser-control/SKILL.md`:
- How to use browser tools (snapshot → ref → action loop)
- Reminder that all browser content is untrusted
- When to take screenshots vs snapshots

**No `pynchy_service_handler()` needed** — the MCP proxy handles security enforcement uniformly.

### 4. Unified Trust Config

Trust declarations for MCP servers move from standalone `[services.*]` into the MCP config path. Three sources, in priority order:

1. **Explicit `[services.*]`** — always wins (for overrides and non-MCP services)
2. **Instance expansion `trust.*`** — per-profile trust in `[mcp_server_instances]`
3. **Plugin defaults** — `trust` dict in `pynchy_mcp_server_spec()` return value

**Implementation:** Trust is extracted from the spec dict before `McpServerConfig` validation (which retains `extra = "forbid"`). The extracted trust is merged into the services registry at startup. `SecurityPolicy._get_trust()` is unchanged — still a string lookup.

**Plugin spec extraction** (in `gateway.py:_collect_plugin_mcp_servers()`):
```python
trust = spec.pop("trust", None)
config = McpServerConfig.model_validate({"type": "script", **spec})
if trust:
    plugin_trust_defaults[name] = ServiceTrustConfig(**trust)
```

**Instance expansion** (in `McpManager._merged_mcp_servers()`):
```python
trust_overrides = {k.removeprefix("trust."): v
                   for k, v in overrides.items() if k.startswith("trust.")}
mcp_kwargs = {k: v for k, v in overrides.items()
              if not k.startswith("trust.")}
```

**Config examples:**

```toml
# Simple — plugin provides trust defaults, user just enables:
[workspaces.research]
mcp_servers = ["browser"]

# Profiles — trust varies per instance:
[mcp_server_instances.browser]
gsuite = { allowed_origins = "https://docs.google.com:*", "trust.public_source" = false, "trust.secret_data" = true }
research = {}  # inherits plugin defaults (public_source=true)

[workspaces.work]
mcp_servers = ["browser.gsuite"]    # public_source=false

[workspaces.research]
mcp_servers = ["browser.research"]  # public_source=true

# Explicit override — wins over plugin/instance:
[services.browser]
public_source = false

# Non-MCP services use [services.*] directly:
[services.x_post]
public_source = false
dangerous_writes = true
```

**Backward compatibility:** Existing `[services.*]` entries keep working and take priority. No migration needed.

## File Changes

### New files

| File | Purpose |
|------|---------|
| `src/pynchy/security/gate.py` | SecurityGate — session-scoped enforcement |
| `src/pynchy/container_runner/_mcp_proxy.py` | aiohttp MCP proxy with fencing |
| `src/pynchy/integrations/plugins/playwright_browser.py` | Plugin: MCP spec + skill paths |
| `container/skills/browser-control/SKILL.md` | Agent skill for browser tools |

### Refactored files

| File | Change |
|------|--------|
| `src/pynchy/ipc/_handlers_service.py` | Replace inline enforcement with `gate.evaluate()` (fixes per-request taint bug) |
| `src/pynchy/container_runner/mcp_manager.py` | Start/stop proxy, return proxy URLs |
| `src/pynchy/container_runner/_orchestrator.py` | Create/destroy gate at container lifecycle, pass `invocation_ts` |
| `src/pynchy/container_runner/gateway.py` | Extract trust from plugin specs, pass to services registry |
| `src/pynchy/group_queue.py` | Carry `invocation_ts` through for gate keying |
| `src/pynchy/config.py` | Merge trust from plugins + instances into services at startup |

### Unchanged

| File | Why |
|------|-----|
| `src/pynchy/security/middleware.py` | SecurityPolicy stays a pure evaluator — gate wraps it |
| `src/pynchy/security/cop.py` | `inspect_inbound()` called by gate, no changes |
| `src/pynchy/config_mcp.py` | McpServerConfig stays infrastructure-only (trust extracted before validation) |

## Browser-Specific Config

Opt-in per workspace via config.toml:

```toml
[workspaces.my_workspace]
mcp_servers = ["browser"]

[workspaces.my_workspace.mcp.browser]
capabilities = ["core"]            # default: core only
allowed_origins = ["https://example.com:*"]
blocked_origins = []
headless = true                    # false enables Xvfb + noVNC
chrome_profile = "work"            # optional, reuses data/chrome-profiles/work/
```

### Capability tiers

Mapped to playwright-mcp's native `capabilities` config:

- **core** (default) — navigate, snapshot, click, type, fill, hover, drag, tabs, wait
- **network** — console messages, network request inspection
- **vision** — coordinate-based mouse actions, screenshots
- **pdf** — PDF generation
- **testing** — locator generation, element verification

Excluded by default (require explicit opt-in):
- **devtools** — `browser_evaluate` (arbitrary JS), `browser_run_code` (arbitrary Playwright code)

## Future Considerations

- **Outbound inspection**: route agent browser actions (navigate, type) through `inspect_outbound()` to catch agents being tricked into dangerous actions
- **noVNC dashboard**: expose live browser view so users can watch agents work
- **Secrets masking**: use playwright-mcp's `secrets` config to prevent agents from seeing sensitive page data
- **Migration of IPC tools to MCP**: X/Google/Slack workflows can become MCP servers, further reducing IPC surface
- **Per-group concurrency relaxation**: if concurrent containers are allowed, gate keying by `(group_folder, invocation_ts)` is already future-proofed

## References

- [Playwright browser plugin design](2026-02-26-playwright-browser-plugin-design.md) — original design this extends
- [Lethal trifecta defenses](2026-02-23-lethal-trifecta-defenses-design.md) — SecurityPolicy, taint model, gating matrix
- [Host-mutating Cop design](2026-02-24-host-mutating-cop-design.md) — Cop inspector integration
- [OpenClaw](https://github.com/openclaw/openclaw) — untrusted content fencing patterns
- [playwright-mcp](https://github.com/microsoft/playwright-mcp) — upstream MCP server
