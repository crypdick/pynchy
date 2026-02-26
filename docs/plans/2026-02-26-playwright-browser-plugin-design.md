# Playwright Browser Plugin Design

**Date:** 2026-02-26
**Status:** Superseded by [MCP Proxy & Security Gate Design](2026-02-26-mcp-proxy-security-gate-design.md)

> **Note:** This document captures the original browser plugin design. The security
> middleware (fencing, Cop integration) and plugin structure have been redesigned as
> part of the centralized MCP proxy. See the superseding doc for the current design.

## Summary

Add general-purpose browser control for pynchy agents via a new plugin wrapping Microsoft's [playwright-mcp](https://github.com/microsoft/playwright-mcp). Agents get browser tools (navigate, snapshot, click, type, etc.) and drive navigation dynamically — replacing the need for hardcoded Playwright workflows.

Inspired by [OpenClaw's](https://github.com/openclaw/openclaw) browser control architecture, particularly its untrusted content fencing and security mediation patterns.

## Goals

- Agents can browse the web autonomously using LLM-driven navigation
- Opt-in per workspace (not globally enabled)
- Security: all browser content treated as untrusted, with fencing + Cop inspection
- Incremental: coexists with existing hardcoded integrations (X, Google, Slack); no refactoring required
- Reuse existing infrastructure: system Chrome, persistent profiles, Xvfb/noVNC

## Non-Goals

- Custom MCP server implementation (we wrap playwright-mcp, not replace it)
- Replacing existing integrations (they coexist; migration is a future decision)
- Multi-format snapshots (playwright-mcp already uses `_snapshotForAI()`, the best format)

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Agent Container (Claude Code)                          │
│                                                         │
│  MCP tools: browser_navigate, browser_snapshot,         │
│  browser_click, browser_type, browser_fill_form, ...    │
│                                                         │
│  ┌───────────────────────┐                              │
│  │ Skills / Directives   │  Browser usage guidance +    │
│  │                       │  untrusted content reminder  │
│  └───────────────────────┘                              │
└──────────────┬──────────────────────────────────────────┘
               │ MCP protocol (via MCP proxy)
               │
┌──────────────┼──────────────────────────────────────────┐
│  Host: Security Middleware                               │
│              │                                           │
│  ┌───────────▼───────────┐                              │
│  │ Untrusted content     │  Fence with random-ID        │
│  │ fencing (always-on)   │  markers + security warning  │
│  └───────────┬───────────┘                              │
│              │                                           │
│  ┌───────────▼───────────┐                              │
│  │ Cop inspect_inbound   │  Haiku scans for injection   │
│  │ (configurable)        │                              │
│  └───┬───────────┬───────┘                              │
│      │           │                                       │
│   CLEAN       FLAGGED → content dropped, generic error  │
│      │                   to agent; full details to       │
│      │                   audit log (invisible to LLM)    │
│      ▼                                                   │
└──────┼──────────────────────────────────────────────────┘
       │
┌──────▼──────────────────────────────────────────────────┐
│  playwright-mcp (script-type MCP server, host-side)      │
│                                                          │
│  - System Chrome (--channel=chrome)                      │
│  - Persistent profile (--user-data-dir)                  │
│  - Xvfb + noVNC when headless=false                      │
│  - Idle timeout auto-stop                                │
└──────────────────────────────────────────────────────────┘
```

## Relationship to Existing Playwright Code

| | Current integrations | New plugin |
|---|---|---|
| **Who drives browser** | Hardcoded Python (host-side) | Agent (LLM) via MCP tools |
| **How it works** | `x_integration.py` calls `page.click("button.tweet")` | Agent calls `browser_click(ref="e3")` |
| **Uses playwright-mcp** | No — Playwright Python directly | Yes |
| **Scope** | Specific workflows (X post, Google OAuth) | General-purpose |
| **Shared infra** | `browser.py` (Chrome, profiles, Xvfb) | Same `browser.py` |

Both coexist. Existing integrations are not touched. Over time, they can optionally migrate to agent-driven skills.

## Plugin Structure

Single file: `src/pynchy/integrations/plugins/playwright_browser.py`

Three hooks:

### `pynchy_mcp_server_spec()`

Provides playwright-mcp as a script-type MCP server:

```python
McpServerConfig(
    type="script",
    command="npx",
    args=["@anthropic-ai/playwright-mcp", "--config", config_path],
    port=...,
    idle_timeout=300,
    transport="streamableHttp",
)
```

Config file dynamically generated per-workspace from config.toml settings.

### `pynchy_service_handler()`

Middleware that:
1. Intercepts browser MCP responses
2. Applies untrusted content fencing (always-on)
3. Routes snapshots through `Cop.inspect_inbound()` (configurable)
4. Drops flagged content; returns generic error to agent

### `pynchy_skill_paths()`

Contributes `container/skills/browser-control/SKILL.md`:
- How to use browser tools (snapshot → ref → action loop)
- Reminder that all browser content is untrusted
- When to take screenshots vs snapshots

## Untrusted Content Fencing

Adapted from OpenClaw's `external-content.ts`. Applied to all browser tool results:

1. **Marker sanitization** — scan content for spoofed boundary markers (including Unicode homoglyph bypasses); replace with `[[MARKER_SANITIZED]]`
2. **Random-ID fences** — wrap with `<<<EXTERNAL_UNTRUSTED_CONTENT id="{random}">>>` ... `<<<END_EXTERNAL_UNTRUSTED_CONTENT id="{random}">>>`. Random ID prevents content from injecting fake end-markers.
3. **Security warning** — prepend notice telling the LLM not to treat content as instructions

## Cop Integration

Browser snapshots routed through `cop.inspect_inbound(source="browser", content=...)`:

- **Clean**: fenced content delivered to agent
- **Flagged**: content dropped entirely. Agent receives: `"Browser content blocked by security policy. The page may contain unsafe content. Try a different page."` Full details (flagged content, Cop reason, URL, group, timestamp) go to:
  - structlog event (invisible to LLM)
  - audit log for human operator

The agent never sees flagged content. No snippet, no reason details, no hints.

## Configuration

Opt-in per workspace via config.toml:

```toml
[sandbox.my_workspace.mcp.playwright]
enabled = true
chrome_profile = "work"            # optional, mounts data/chrome-profiles/work/
capabilities = ["core"]            # default: core only
allowed_origins = ["https://example.com:*"]
blocked_origins = []
headless = true                    # false enables Xvfb + noVNC
cop_enabled = true                 # default: true
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

### Chrome profiles

Reuse existing `data/chrome-profiles/{name}/` directories managed by `browser.py`. An agent can inherit auth state from a profile set up via existing integrations.

## How Snapshots Work

playwright-mcp uses Playwright's `_snapshotForAI()` — the same private API that OpenClaw uses for its best snapshot format. Returns an LLM-optimized text representation of the page with element refs:

```
- button "Submit" [ref=e1]
- textbox "Email" [ref=e2]
- link "Sign up" [ref=e3]
```

Supports incremental mode (only diffs from last snapshot) for token savings. Configured via `snapshot.mode: 'incremental' | 'full' | 'none'`.

No need for OpenClaw's multi-format system — playwright-mcp's single format is already the best one.

## Next Step: Implementation Plan

This design is approved. The next step is to create a detailed implementation plan.

### 1. Clone reference repos (ephemeral, not checked in)

These repos contain reference implementations. Clone them before starting:

```bash
# OpenClaw — security fencing patterns (external-content.ts), browser tool wrapping
git clone --depth 1 https://github.com/openclaw/openclaw.git /tmp/openclaw

# Playwright monorepo — playwright-mcp source lives here (compiled JS in playwright-mcp repo)
git clone --depth 1 --filter=blob:none --sparse https://github.com/microsoft/playwright.git /tmp/playwright
cd /tmp/playwright && git sparse-checkout set packages/playwright/src/mcp && cd -

# playwright-mcp repo — config types, tests, package structure
git clone --depth 1 https://github.com/microsoft/playwright-mcp.git /tmp/playwright-mcp
```

### 2. Run writing-plans skill

```
/writing-plans docs/plans/2026-02-26-playwright-browser-plugin-design.md
```

### 3. Key references

**In pynchy (always available):**
- `src/pynchy/integrations/plugins/x_integration.py` — existing plugin with MCP spec + service handler + skills (pattern to follow)
- `src/pynchy/container_runner/mcp_manager.py` — how script-type MCP servers are started/stopped
- `src/pynchy/security/cop.py` — `inspect_inbound()` for injection scanning
- `src/pynchy/integrations/browser.py` — system Chrome detection, persistent profiles, Xvfb/noVNC
- `src/pynchy/config_mcp.py` — `McpServerConfig` model for script-type servers
- `container/skills/` — existing skills with YAML frontmatter

**In cloned repos (from step 1):**
- `/tmp/openclaw/src/security/external-content.ts` — untrusted content fencing to port (marker sanitization, random-ID fences, Unicode homoglyph defense, security warning)
- `/tmp/openclaw/src/agents/tools/browser-tool.ts` lines 448-590 — how OpenClaw wraps browser snapshots before sending to LLM
- `/tmp/playwright/packages/playwright/src/mcp/browser/tab.ts` — `captureSnapshot()` using `_snapshotForAI()`, incremental snapshot support
- `/tmp/playwright/packages/playwright/src/mcp/browser/tools/snapshot.ts` — tool definitions (click, hover, drag, etc.) and element ref resolution
- `/tmp/playwright-mcp/packages/playwright-mcp/config.d.ts` — full Config type (capabilities, network origins, snapshot mode, secrets)

## Future Considerations

- **Outbound Cop inspection**: optionally route agent browser actions (navigate, type) through `inspect_outbound()` to catch agents being tricked into dangerous actions (e.g., "type the API key into this form")
- **Migration of existing integrations**: X/Google/Slack workflows can become skills that use browser tools instead of hardcoded Python
- **noVNC dashboard**: expose live browser view so users can watch agents work and intervene
- **Secrets masking**: use playwright-mcp's `secrets` config to prevent agents from seeing sensitive data on pages
