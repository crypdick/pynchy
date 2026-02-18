# LiteLLM MCP Server Management API

LiteLLM docs: https://docs.litellm.ai/docs/mcp

## Critical: Two `/mcp/` Route Families

LiteLLM exposes two sets of routes under `/mcp/` that look similar but serve completely different purposes:

| Route family | Purpose | Content-Type |
|---|---|---|
| `/mcp/*` | SSE/streamable-HTTP **transport** — what MCP *clients* connect to | `text/event-stream` |
| `/v1/mcp/server` | REST **management** API — CRUD for server configs | `application/json` |

Hitting `/mcp/server/...` (missing `/v1/` prefix) returns a JSONRPC 406:
```json
{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,"message":"Not Acceptable: Client must accept both application/json and text/event-stream"}}
```

This is the SSE transport rejecting a plain JSON request — not a meaningful error about your payload.

## REST API Endpoints

All endpoints require `Authorization: Bearer <master_key>`.

### List servers

```bash
curl -s -H "Authorization: Bearer $KEY" http://localhost:4000/v1/mcp/server
```

Returns a **bare JSON array** `[{...}, ...]`, not `{"data": [...]}`.

### Create server

```bash
curl -s -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  http://localhost:4000/v1/mcp/server \
  -d '{"server_name": "playwright", "url": "http://pynchy-mcp-playwright:8931", "transport": "sse"}'
```

Field gotchas:
- **`url`** not `server_url` — the field name differs from what you might expect
- **`server_name` cannot contain hyphens** — LiteLLM rejects names like `my-server`; use underscores instead
- LiteLLM does **not** validate the URL at registration time — the server doesn't need to be running yet

### Delete server

```bash
curl -s -X DELETE -H "Authorization: Bearer $KEY" \
  http://localhost:4000/v1/mcp/server/<server_id_uuid>
```

Delete requires the **UUID `server_id`**, not the `server_name`. Get the UUID from the list endpoint first.

## Teams & Virtual Keys (MCP Access Control)

LiteLLM teams restrict which MCP servers a key can access. Pynchy creates one team per workspace.

```bash
# Create team
curl -s -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  http://localhost:4000/team/new \
  -d '{"team_alias": "pynchy-mcp-admin"}'

# Create key with MCP access
curl -s -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  http://localhost:4000/key/generate \
  -d '{"team_id": "<team_uuid>", "allowed_mcp_servers": ["playwright"]}'
```

Note: team/key endpoints use `/team/new` and `/key/generate` (no `/v1/` prefix) — unlike the MCP server endpoints.

## Debugging MCP Registration

When MCP registration fails at boot, check pynchy logs for:

```bash
ssh pynchy 'journalctl --user -u pynchy --since "5 min ago" --no-pager' | grep -i mcp
```

Common log patterns:

| Log message | Meaning |
|---|---|
| `No MCP servers configured — skipping MCP sync` | No `[mcp_servers.*]` sections in config.toml |
| `No workspaces reference MCP servers — skipping MCP sync` | Servers defined but no workspace has `mcp_servers = [...]` |
| `Syncing MCP state to LiteLLM` | Happy path — sync starting |
| `Failed to register MCP endpoint` + 406 | Hitting `/mcp/` transport instead of `/v1/mcp/server` |
| `Starting MCP container on-demand` | Docker MCP container launching for agent use |
| `MCP container failed health check` | Container started but HTTP health probe failed within 60s |
