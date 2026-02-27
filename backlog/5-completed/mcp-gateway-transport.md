# MCP gateway transport: Claude SDK cannot connect to LiteLLM

## Problem

Remote MCP tools (e.g. Slack) are registered with LiteLLM and the Docker
containers start correctly, but the Claude SDK inside agent containers cannot
connect to LiteLLM's `/mcp/` endpoint.

LiteLLM's MCP endpoint only speaks **Streamable HTTP** (POST-based JSON-RPC).
The Claude SDK's `type: "http"` transport hangs during initialization
(60s timeout → `Control request timeout: initialize`), crashing the agent.
`type: "sse"` fails gracefully (tools server shows "failed", agent still works)
but SSE isn't supported by LiteLLM's `/mcp/` endpoint either.

## Diagnostic evidence

**LiteLLM endpoint responds correctly to manual curl:**
```bash
curl -s -X POST \
  -H "Authorization: Bearer <virtual-key>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","method":"initialize","id":1,...}' \
  "http://localhost:4000/mcp/"
# Returns: event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{...}}
```

**Same endpoint reachable from inside Docker containers:**
```bash
docker run --rm --add-host host.docker.internal:host-gateway curlimages/curl \
  <same curl as above> http://host.docker.internal:4000/mcp/
# Works
```

**Claude SDK with `type: "http"` hangs:**
- Agent container logs show no MCP connection errors — just a 60s timeout
- Error: `Failed to start agent core: Control request timeout: initialize`
- Tested with Claude Code v2.1.45 and v2.1.49

**Claude SDK with `type: "sse"` fails fast (graceful):**
- Init data shows: `"mcp_servers": [{"name": "tools", "status": "failed"}]`
- Agent starts and works, just without remote MCP tools

**LiteLLM SSE (GET) returns 400:**
```
{"jsonrpc":"2.0","error":{"code":-32600,"message":"Bad Request: Missing session ID"}}
```

## Auth notes (resolved)

LiteLLM MCP auth requires `Bearer ` prefix. Use `Authorization: Bearer <key>`,
not bare `x-litellm-api-key: <key>`. This is already fixed in the codebase.

## Current workaround

`type: "sse"` in `container/agent_runner/src/agent_runner/main.py:267`.
Agent starts fine but remote MCP tools are unavailable.

## Resolution

**Resolved by MCP proxy (2026-02-26).** Instead of fixing LiteLLM transport,
we bypass LiteLLM entirely for MCP. The new MCP proxy in McpManager routes
container MCP traffic through `SecurityGate` with path-based routing
(`/mcp/{group_folder}/{invocation_ts}/{instance_id}`). Containers connect to
the proxy via `host.docker.internal`, which forwards to backend MCP servers.

This eliminates the LiteLLM transport incompatibility and adds security
enforcement (taint tracking, fencing, Cop inspection) that LiteLLM couldn't
provide.

See: `docs/plans/2026-02-26-mcp-proxy-security-gate-design.md`

## Investigation paths (historical)

1. **Claude SDK bug** — test with newer SDK versions as they release. The HTTP
   transport might have a bug with SSE-encoded responses (LiteLLM returns
   `event: message\ndata: ...` format for Streamable HTTP).
2. **Bypass LiteLLM** — connect agent directly to MCP containers via their
   published host ports (`http://host.docker.internal:<port>/mcp`). Loses
   LiteLLM auth/routing but simpler transport. Requires agent containers to
   join `pynchy-litellm-net` or use published ports.
3. **SSE adapter** — add a thin SSE↔HTTP adapter in front of LiteLLM that
   translates SSE connections to Streamable HTTP. Overkill unless paths 1–2
   fail.

## Files

- `container/agent_runner/src/agent_runner/main.py:265-280` — MCP gateway config
- `src/pynchy/container_runner/_orchestrator.py:193-212` — gateway URL/key injection
- `src/pynchy/container_runner/mcp_manager.py` — MCP lifecycle manager
