---
name: LiteLLM
description: Use when interacting with the LiteLLM proxy — investigating failed requests, model routing errors, spend tracking, health checks, API gateway diagnostics, or modifying the LiteLLM configuration. Also use when the user mentions the LiteLLM UI, dashboard, proxy errors, or model availability.
---

# LiteLLM Gateway

The LiteLLM proxy runs as a Docker container (`pynchy-litellm`) managed by pynchy with a PostgreSQL sidecar (`pynchy-litellm-db`). Accessible at `http://localhost:4000` on the pynchy server, or `http://pynchy.asymptote-shilling.ts.net:4000` via Tailscale.

## Authentication

The master key is in `config.toml` on the pynchy server under `[gateway].master_key`:

```bash
ssh pynchy 'grep master_key ~/src/PERSONAL/pynchy/config.toml'
```

Pass as Bearer token: `-H "Authorization: Bearer <key>"`

## Configuration

Config file: `~/src/PERSONAL/pynchy/litellm_config.yaml` on the pynchy server. Uses wildcard routing (`anthropic/*`, `openai/*`) so any model from a provider works without explicit entries.

Editing the config file triggers an automatic service restart (~30-90s). Do not manually restart containers.

## UI

Dashboard: `http://pynchy.asymptote-shilling.ts.net:4000/ui/`

## MCP Server Management

LiteLLM can proxy MCP tool servers to agents. Pynchy registers/deregisters these via `McpManager` at boot. For the MCP management REST API and known gotchas, see [references/mcp-api.md](references/mcp-api.md).

## Diagnostics

For detailed API endpoints, analysis patterns, and common failure troubleshooting, see [references/diagnostics.md](references/diagnostics.md).
