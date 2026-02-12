# Tailscale Deploy Endpoint

## Context

You want to edit the pynchy repo remotely over Tailscale (SSH + Claude Code), commit changes, then hit an HTTP endpoint to make pynchy pull and restart — with a WhatsApp message confirming success or rolling back on failure. The existing `deploy_changes` MCP tool handles deploys initiated *from within* an agent conversation. This adds the external HTTP counterpart.

## What We're Building

An `aiohttp` web server embedded in the pynchy process, bound to `0.0.0.0` on a configurable port. On startup it validates that Tailscale is up. Exposes two endpoints:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Service health check (uptime, git SHA, WhatsApp connected) |
| `/deploy` | POST | `git pull` → validate → restart → WhatsApp notification |

## Deploy Flow

```
POST /deploy
  ├─ Record current HEAD sha
  ├─ git pull --ff-only
  │   └─ fail? → respond 409, notify WhatsApp, done
  ├─ Validate: python -c "import pynchy"
  │   └─ fail? → git reset --hard {old_sha}, respond 422, notify WhatsApp, done
  ├─ Write deploy_continuation.json (reuses existing mechanism)
  ├─ Respond 200 with {sha, previous_sha, status: "restarting"}
  ├─ Notify WhatsApp main group: "Deploying {sha}... restarting now."
  └─ SIGTERM self → service manager restarts → continuation resumes
```

On restart, the existing `_auto_rollback` in `app.py` handles startup failures (git reset + WhatsApp notification). The existing `_check_deploy_continuation` injects a synthetic message so the agent confirms health.

## Files to Create/Modify

### New: `src/pynchy/http_server.py` (~80 lines)
- `async def start_http_server(deps: HttpDeps) -> aiohttp.web.AppRunner`
- Deps protocol: `send_message(jid, text)`, `main_chat_jid() -> str`, `channels_connected() -> bool`
- `/health` handler — returns JSON with uptime, HEAD sha, connected status
- `/deploy` handler — pull, validate, continuation, SIGTERM (reuses logic from `ipc.py:_handle_deploy`)
- Binds to `0.0.0.0:{DEPLOY_PORT}` (default 8484)

### Modify: `src/pynchy/config.py`
- Add `DEPLOY_PORT: int` env var (default 8484)

### Modify: `src/pynchy/app.py`
- Import and start the HTTP server as an asyncio task alongside scheduler/IPC
- Validate Tailscale is up during startup (`tailscale status --json` via subprocess)
- Provide deps adapter (same pattern as `_make_ipc_deps`)
- Graceful shutdown: stop the HTTP runner in `_shutdown()`

### Modify: `pyproject.toml`
- Add `aiohttp` dependency

### Update: backlog files
- Move tailscale-integration.md → planning/in-progress status
- Mark "External pull & restart" done in small-improvements.md

## Design Decisions

**Why `aiohttp` over FastAPI/Starlette?** Minimal dependency, native asyncio, no ASGI complexity. Two endpoints don't need OpenAPI/Pydantic validation. Fits the "small enough to understand" philosophy.

**Why `--ff-only`?** Prevents merge commits from remote edits conflicting with local state. If the pull can't fast-forward, it fails cleanly rather than creating a mess.

**Why validate with `import pynchy`?** Catches syntax errors and missing dependencies before restarting. Cheap, fast, and catches the most common remote-edit mistakes.

**Why bind to `0.0.0.0` instead of detecting the Tailscale IP?** The Tailscale IP (`100.x.x.x`) is stable but detecting it adds complexity. Binding to `0.0.0.0` works because the machine's firewall + Tailscale ACLs control access. The port is non-standard (8484) and only meaningful within the tailnet.

**Tailscale validation:** On startup, run `tailscale status --json` and log a warning if Tailscale isn't connected. Non-fatal — the service should still run for WhatsApp even if Tailscale is down.

## Implementation Sequence

1. `uv add aiohttp` — add dependency
2. Add `DEPLOY_PORT` to `config.py`
3. Create `http_server.py` with health + deploy endpoints
4. Wire into `app.py` startup/shutdown
5. Add Tailscale validation to startup
6. Update backlog
7. Test: `curl http://localhost:8484/health` and `curl -X POST http://localhost:8484/deploy`

## Verification

1. `uv run pytest tests/` — existing tests still pass
2. `uv run ruff check --fix src/` — lint clean
3. Manual: start service, curl `/health`, verify JSON response
4. Manual: make a commit, curl `/deploy`, verify WhatsApp message + restart
5. Manual: break an import, curl `/deploy`, verify rollback + error message
