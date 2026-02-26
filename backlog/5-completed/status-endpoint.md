# Status Endpoint (`GET /status`)

Comprehensive operational status endpoint on the existing HTTP server (port 8484). Aggregates health from all subsystems into a single JSON response, queryable from any device on the tailnet via `curl pynchy-server:8484/status`.

## Problem

Diagnosing pynchy currently requires SSH + a grab-bag of shell commands: `systemctl status`, `docker ps`, `journalctl --grep`, `sqlite3` queries, `git status` across worktrees, and `curl` to the LiteLLM proxy. The [server-debug skill](/.claude/skills/pynchy-ops/references/server-debug.md) documents these commands, but they're manual, slow, and fragile (log greps break when log formats change).

A single HTTP endpoint replaces all of this with structured data that's trivially consumable by humans (`curl | jq`), monitoring scripts, or a future dashboard.

## Context

### Existing infrastructure

- **HTTP server** (`http_server.py`) — aiohttp on port 8484, already has `/health` (basic) and `/deploy`. Uses `HttpDeps` protocol for dependency injection, wired via `dep_factory.py`.
- **`/health` endpoint** — Returns uptime, HEAD SHA, dirty flag, channels_connected. Too shallow for real debugging.
- **Git helpers** (`git_ops/utils.py`) — `get_head_sha()`, `is_repo_dirty()`, `count_unpushed_commits()`, `run_git()`.
- **GroupQueue** (`group_queue.py`) — In-memory per-group state: active flag, pending messages/tasks, process reference.
- **SQLite DB** — `messages`, `outbound_ledger`, `outbound_deliveries`, `scheduled_tasks`, `host_jobs`, `sessions`, `router_state` tables.
- **LiteLLM gateway** (`container_runner/gateway.py`) — Docker container `pynchy-litellm` + Postgres sidecar `pynchy-litellm-db`. Has `/health` endpoint.
- **Channel protocol** (`types.py`) — `Channel.is_connected()` per channel.

### Subsystems to report on

| Subsystem | What "healthy" means |
|-----------|---------------------|
| Service | Running, not shutting down, uptime reasonable |
| Channels | All channels connected to upstream services |
| LLM Gateway | LiteLLM + Postgres containers running, model endpoints healthy |
| Container Queue | Containers draining, not stuck at concurrency limit |
| Git / Repos / Worktrees | Main repo clean, worktrees not conflicted or diverged |
| Messages | Inbound and outbound flowing, no stuck pending deliveries |
| Scheduled Tasks | Tasks running on schedule, none overdue or stuck |
| Host Jobs | Cron jobs executing, none overdue |

## Plan

### Response shape

```json
{
  "service": {
    "status": "ok",
    "started_at": "2026-02-20T09:00:00+00:00",
    "uptime_seconds": 28800
  },

  "deploy": {
    "head_sha": "5fda35b7abc...",
    "head_commit": "fix: deduplicate message routing",
    "dirty": false,
    "unpushed_commits": 0,
    "last_deploy_at": "2026-02-20T08:30:00+00:00",
    "last_deploy_sha": "abc123def..."
  },

  "channels": {
    "whatsapp": { "connected": true },
    "slack": { "connected": false }
  },

  "gateway": {
    "mode": "litellm",
    "litellm_container": "running",
    "postgres_container": "running",
    "healthy_models": 1,
    "unhealthy_models": 0
  },

  "queue": {
    "active_containers": 1,
    "max_concurrent": 10,
    "groups_waiting": 0,
    "per_group": {
      "admin": {
        "active": true,
        "is_task": false,
        "pending_messages": true,
        "pending_tasks": 0
      },
      "code-improver": {
        "active": false,
        "is_task": false,
        "pending_messages": false,
        "pending_tasks": 1
      }
    }
  },

  "repos": {
    "crypdick/pynchy": {
      "head_sha": "5fda35b7abc...",
      "dirty": false,
      "unpushed_commits": 0,
      "worktrees": {
        "code-improver": {
          "sha": "a1b2c3d4...",
          "dirty": true,
          "ahead": 2,
          "behind": 0,
          "conflict": false
        }
      }
    }
  },

  "messages": {
    "total_inbound": 812,
    "total_outbound": 212,
    "last_received_at": "2026-02-20T09:48:00+00:00",
    "last_sent_at": "2026-02-20T09:47:30+00:00",
    "pending_deliveries": 0
  },

  "tasks": [
    {
      "id": "abc-123",
      "group": "admin",
      "schedule_type": "cron",
      "schedule_value": "0 9 * * *",
      "status": "active",
      "next_run": "2026-02-21T09:00:00+00:00",
      "last_run": "2026-02-20T09:00:12+00:00",
      "last_result": "ok"
    }
  ],

  "host_jobs": [
    {
      "id": "def-456",
      "name": "backup-db",
      "schedule_type": "cron",
      "schedule_value": "0 3 * * *",
      "status": "active",
      "enabled": true,
      "next_run": "2026-02-21T03:00:00+00:00",
      "last_run": "2026-02-20T03:00:05+00:00"
    }
  ],

  "groups": {
    "total": 5,
    "active_sessions": 2
  }
}
```

### Data sources

| Section | Source | Blocking? |
|---------|--------|-----------|
| `service` | New `_started_at = datetime.now(UTC)` at module level + existing `_start_time` (monotonic). `HttpDeps.is_shutting_down()`, `HttpDeps.channels_connected()` | No — in-memory |
| `deploy` | `get_head_sha()`, `is_repo_dirty()`, `count_unpushed_commits()`, `get_head_commit_message()`. **New**: `router_state` rows `last_deploy_at` and `last_deploy_sha`, written by `finalize_deploy` | Git: ~40ms subprocess. DB: async |
| `channels` | Iterate `app.channels`, call `ch.is_connected()` per channel | No — in-memory |
| `gateway` | Mode from `get_settings().gateway`. Container status via `is_container_running("pynchy-litellm")` and `is_container_running("pynchy-litellm-db")`. Model health via HTTP GET to `localhost:{port}/health` | Docker inspect: ~20ms. HTTP: ~50ms |
| `queue` | Direct read of `GroupQueue._groups`, `_active_count`, `_waiting_groups` | No — in-memory |
| `repos` | Iterate `config.repos` → `get_repo_context()`. Per repo: `get_head_sha(cwd)`, `is_repo_dirty(cwd)`, `count_unpushed_commits(cwd)`. Per worktree: SHA, dirty, ahead/behind via `git rev-list --count`, conflict via `MERGE_HEAD`/`REBASE_HEAD` file existence | ~80ms subprocess (1 repo, 3 worktrees). Run in `asyncio.to_thread()` |
| `messages` | DB: `COUNT(*) FROM messages WHERE is_from_me = 0` (inbound), `COUNT(*) FROM outbound_ledger` (outbound), `MAX(timestamp) FROM messages WHERE is_from_me = 0` (last received), `MAX(timestamp) FROM outbound_ledger` (last sent), `COUNT(*) FROM outbound_deliveries WHERE delivered_at IS NULL` (pending) | Async DB, ~10ms |
| `tasks` | DB: `SELECT id, group_folder, schedule_type, schedule_value, status, next_run, last_run, last_result FROM scheduled_tasks` — full list, one row per task | Async DB |
| `host_jobs` | DB: `SELECT id, name, schedule_type, schedule_value, status, enabled, next_run, last_run FROM host_jobs` — full list | Async DB |
| `groups` | `len(workspaces)` + `len(active_sessions)` | No — in-memory |

### Implementation steps

#### Step 1: Move `_get_head_commit_message` to `git_ops/utils.py`

Currently lives in `http_server.py` but it's a git helper. Both `/health` and `/status` need it. Move it to `git_ops/utils.py` as `get_head_commit_message()`, update the import in `http_server.py`.

#### Step 2: Persist deploy timestamps

In `_handle_deploy()` (http_server.py) and `finalize_deploy()` (deploy.py), write two `router_state` rows:
- `last_deploy_at` → `datetime.now(UTC).isoformat()`
- `last_deploy_sha` → the new SHA

Read them back in the status collector via existing `get_router_state()`.

#### Step 3: New `src/pynchy/status.py`

Pure data-collection module. No HTTP logic.

```python
async def collect_status(deps: StatusDeps) -> dict[str, Any]:
    """Gather operational status from all subsystems."""
    ...
```

Orchestration:
1. Fire all independent checks concurrently: `asyncio.gather()` for async checks (DB queries, gateway HTTP), `asyncio.to_thread()` for blocking checks (git subprocess, docker inspect).
2. Assemble the dict from results.
3. Each section has its own private collector function (e.g. `_collect_repos()`, `_collect_messages()`, `_collect_gateway()`) for readability and testability.

#### Step 4: `StatusDeps` protocol

New protocol in `status.py` (or extend `HttpDeps`):

```python
class StatusDeps(Protocol):
    def is_shutting_down(self) -> bool: ...
    def get_channel_status(self) -> dict[str, bool]: ...
    def get_queue_snapshot(self) -> dict[str, Any]: ...
    def get_gateway_info(self) -> dict[str, Any]: ...
    def get_active_sessions(self) -> dict[str, str]: ...
    def get_workspace_count(self) -> int: ...
```

Everything else (git, DB, config) is accessed directly — no dep injection needed for read-only queries to shared resources.

#### Step 5: Wire deps in `dep_factory.py`

Add to `make_http_deps()`:
- `get_channel_status` → iterates `app.channels`, returns `{ch.name: ch.is_connected()}`
- `get_queue_snapshot` → reads `app.queue._groups`, `._active_count`, `._waiting_groups`
- `get_gateway_info` → reads from gateway singleton (`get_gateway()`)
- `get_workspace_count` → `len(app.workspaces)`

#### Step 6: HTTP handler + route registration

In `http_server.py`:
- Add `_started_at = datetime.now(UTC)` alongside existing `_start_time = time.monotonic()`
- Add `async def _handle_status(request)` → calls `collect_status(deps)`, returns `web.json_response()`
- Register `GET /status` in `start_http_server()`

#### Step 7: Refactor `/health` (optional)

The existing `/health` endpoint could delegate to a subset of `/status` for backwards compatibility, or just stay as-is. Low priority — `/health` is intentionally lightweight and used by automated checks.

### Worktree status detail

For each worktree under `repo_ctx.worktrees_dir`:

```python
def _worktree_status(worktree_path: Path, main_branch: str, repo_root: Path) -> dict:
    sha = get_head_sha(cwd=worktree_path)
    dirty = is_repo_dirty(cwd=worktree_path)
    branch = f"worktree/{worktree_path.name}"

    # Ahead/behind relative to main
    ahead = run_git("rev-list", f"{main_branch}..{branch}", "--count", cwd=repo_root)
    behind = run_git("rev-list", f"{branch}..{main_branch}", "--count", cwd=repo_root)

    # Conflict detection: MERGE_HEAD or REBASE_HEAD presence
    git_dir = worktree_path / ".git"  # file pointing to actual git dir
    conflict = (worktree_path / "MERGE_HEAD").exists() or (worktree_path / "REBASE_HEAD").exists()
    # For worktrees, .git is a file — check the actual git dir too
    if not conflict:
        actual_git_dir = run_git("rev-parse", "--git-dir", cwd=worktree_path)
        if actual_git_dir.returncode == 0:
            gd = Path(actual_git_dir.stdout.strip())
            conflict = (gd / "MERGE_HEAD").exists() or (gd / "REBASE_HEAD").exists()

    return {
        "sha": sha,
        "dirty": dirty,
        "ahead": int(ahead.stdout.strip()) if ahead.returncode == 0 else None,
        "behind": int(behind.stdout.strip()) if behind.returncode == 0 else None,
        "conflict": conflict,
    }
```

### Gateway health detail

For LiteLLM mode, issue a local HTTP call:

```python
async def _collect_gateway(info: dict) -> dict:
    result = {"mode": info["mode"]}
    if info["mode"] == "litellm":
        result["litellm_container"] = _container_state("pynchy-litellm")
        result["postgres_container"] = _container_state("pynchy-litellm-db")
        # Query LiteLLM /health for model counts
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.get(
                    f"http://localhost:{info['port']}/health",
                    headers={"Authorization": f"Bearer {info['key']}"},
                    timeout=aiohttp.ClientTimeout(total=5),
                )
                data = await resp.json()
                result["healthy_models"] = data.get("healthy_count", 0)
                result["unhealthy_models"] = data.get("unhealthy_count", 0)
        except Exception:
            result["healthy_models"] = None
            result["unhealthy_models"] = None
    return result

def _container_state(name: str) -> str:
    """Return 'running', 'stopped', or 'not_found'."""
    result = run_docker("inspect", "-f", "{{.State.Status}}", name, check=False)
    status = result.stdout.strip()
    if result.returncode != 0:
        return "not_found"
    return status  # "running", "exited", "created", etc.
```

### Performance budget

| Check | Cost | Approach |
|-------|------|----------|
| Service / queue / groups / channels | ~0ms | In-memory reads |
| Deploy info (4 git subprocess calls) | ~40ms | `asyncio.to_thread()` |
| Gateway containers (2 docker inspect) | ~20ms | `asyncio.to_thread()` |
| Gateway health (HTTP to localhost) | ~50ms | `aiohttp` async |
| Repos + worktrees (~8 git subprocess) | ~80ms | `asyncio.to_thread()` |
| DB queries (messages, tasks, host_jobs) | ~10ms | Async SQLite |
| **Total** | **~200ms** | All I/O concurrent via `gather()` |

### Replaces these debug commands

From [server-debug.md](/.claude/skills/pynchy-ops/references/server-debug.md):

| Debug command | Status section |
|--------------|----------------|
| `systemctl --user status pynchy` | `service.status`, `service.uptime_seconds` |
| `docker ps --filter name=pynchy` | `gateway.litellm_container`, `gateway.postgres_container` |
| `journalctl --grep 'Connected to WhatsApp'` | `channels.whatsapp.connected` |
| `journalctl --grep 'groupCount'` | `groups.total` |
| `grep 'Container active\|concurrency limit' logs/` | `queue.active_containers`, `queue.per_group` |
| `grep 'New messages' logs/` | `messages.last_received_at` |
| `sqlite3 ... SELECT MAX(timestamp) FROM messages` | `messages.last_received_at` |
| `sqlite3 ... SELECT FROM scheduled_tasks` | `tasks` (full list) |
| `git status --porcelain` | `deploy.dirty`, `repos.*.dirty` |
| `git worktree list` + per-worktree inspection | `repos.*.worktrees` |

From [litellm-diagnostics.md](/.claude/skills/pynchy-ops/references/litellm-diagnostics.md):

| Debug command | Status section |
|--------------|----------------|
| `curl localhost:4000/health` | `gateway.healthy_models`, `gateway.unhealthy_models` |
| `docker logs pynchy-litellm` | Not included (log content is too variable for structured status) |

### Deferred to v2

- **LiteLLM spend/budget** — Requires querying LiteLLM Postgres or Anthropic billing API. Fragile external dependency. Better as a cached periodic check or separate endpoint.
- **MCP container status** — `docker inspect` per MCP container adds latency proportional to MCP count. Queue status already shows active work. Can add an `mcp` section later.
- **Task run history / error rates** — `task_run_logs` table has this data but per-task history would bloat the response. Available via a future `GET /status/tasks/:id`.
- **Log scanning** — The server-debug reference greps log files for patterns (timeouts, retries, mount rejections). This is inherently fragile and better done interactively. Structured logging could feed counters into `/status` later.
- **Per-group last-message time** — Would require a join or subquery per group. Global `last_received_at` / `last_sent_at` covers "is anything happening?". Per-group can be added to the `queue.per_group` section if needed.

## Done

All implementation steps complete:

1. **Step 1** — `get_head_commit_message()` moved to `git_ops/utils.py` (shared by `/health` and `/status`).
2. **Step 2** — Deploy timestamps (`last_deploy_at`, `last_deploy_sha`) persisted in `router_state` by `finalize_deploy()`.
3. **Step 3** — `src/pynchy/status.py` — pure data-collection module with per-section collectors, concurrent I/O via `asyncio.gather()`.
4. **Step 4** — `StatusDeps` protocol defined in `status.py`.
5. **Step 5** — Deps wired in `dep_factory.py:make_status_deps()`, passed to `start_http_server()`.
6. **Step 6** — `_handle_status` handler + `GET /status` route registered. `record_start_time()` called at boot.
7. **Step 7** — `/health` left as-is (intentionally lightweight for automated checks).

Also fixed: circular import between `deploy.py` ↔ `ipc._handlers_deploy` (moved to lazy import).

Tests: `tests/test_status.py` — 24 tests covering all collectors, the HTTP endpoint, error paths, and edge cases.
