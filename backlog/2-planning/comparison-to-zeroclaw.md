Here's an analysis of the zeroclaw design decisions that would most benefit pynchy, ordered by impact:

---

## 1. Unified Observability Trait (High Impact)

Zeroclaw has an `Observer` trait with pluggable backends (noop, log, OTel, multi-observer). Events like `AgentStart`, `AgentEnd`, `ToolCall`, and metrics like `RequestLatency`, `TokensUsed` are recorded throughout the agent loop.

**Pynchy today:** Has structlog and the `EventBus`, but they serve different purposes — structlog is for human-readable logs, and EventBus is for internal wiring (triggering side-effects). There's no structured telemetry layer that could answer "how long did agent turns take this week?" or "which tools are slowest?"

**What to adopt:** A lightweight `Observer` protocol that sits between the EventBus and structlog. Record durations and outcomes at key points (container spawn, agent turn, IPC round-trip, tool execution). Start with a log-based backend; the abstraction makes OTel a later drop-in. This would give you real operational visibility without changing the existing logging or event bus.

---

## 2. Component Health Registry (High Impact)

Zeroclaw tracks every subsystem (gateway, channels, scheduler, heartbeat) in a `HealthRegistry` singleton with `mark_component_ok()` / `mark_component_error()` / `bump_component_restart()`. The `/health` endpoint returns a structured snapshot.

**Pynchy today:** The `/health` endpoint returns uptime and git SHA, plus channel connection status. There's no tracking of whether IPC is healthy, containers are responsive, the DB is reachable, or the queue is draining.

**What to adopt:** A `HealthRegistry` that each subsystem reports into. The existing `/health` endpoint would return component-level status, making it trivial to spot "IPC watcher died 5 minutes ago" vs. "everything is fine." This is especially valuable since pynchy runs as a long-lived daemon where silent subsystem failures are the hardest bugs to diagnose.

---

## 3. Resilient Provider Wrapper (Medium-High Impact)

Zeroclaw's `ReliableProvider` wraps any provider with retry + exponential backoff + failover to the next provider. It distinguishes retryable errors (429, 5xx, timeouts) from non-retryable ones (400, 401, 403). There's also a `RouterProvider` that routes `hint:reasoning` to one provider and `hint:fast` to another.

**Pynchy today:** Provider logic lives inside the container. The host has no visibility into or control over LLM failures. If Claude returns a 500, the container process fails, the host sees an error exit code, and the queue retry kicks in — restarting the entire container for what might have been a transient API blip.

**What to adopt:** At minimum, the container-side `AgentCore` implementations should have built-in retry with backoff for transient API errors, so a single 429 doesn't kill a 10-minute agent session. The hint-based routing pattern is also worth considering — it would let workspace configs specify `model: hint:fast` or `model: hint:reasoning` and resolve to actual provider+model combinations centrally.

---

## 4. Formalized Security Policy Engine (Medium Impact)

Zeroclaw has a single `SecurityPolicy` struct that owns autonomy levels, command allowlists, path validation, rate limiting, and risk classification. Every tool execution runs through `policy.check_command()`.

**Pynchy today:** Security is effective but scattered — `mount_security.py` handles mount validation, workspace profiles have `pynchy_repo_access` levels, IPC handlers check authorization, and you have planned-but-not-started security hardening in the backlog. The logic works, but there's no single place to answer "what can this group do?"

**What to adopt:** A `SecurityPolicy` dataclass per workspace/group that consolidates the rules. Instead of checking mounts in one place and IPC authorization in another, the policy object is the single source of truth. This would also make the planned security profiles (`backlog/2-planning/security-hardening-1-profiles.md`) much easier to implement — each profile is just a named policy preset.

---

## 5. Component Supervision with Backoff (Medium Impact)

Zeroclaw's `spawn_component_supervisor()` wraps each subsystem in a loop with exponential backoff. If a channel crashes, it restarts automatically. If it keeps crashing, backoff grows. Health is updated at each transition.

**Pynchy today:** Relies on systemd/launchd for process-level restarts and has graceful shutdown logic. But if, say, the IPC watcher task panics or the WhatsApp channel disconnects, there's no internal mechanism to restart just that component. The whole process stays up but is partially broken.

**What to adopt:** A `supervise()` async utility that wraps `asyncio.create_task()` calls. Something like:

```python
async def supervise(name: str, coro_factory, *, base_backoff=1.0, max_backoff=60.0):
    backoff = base_backoff
    while True:
        health.mark_ok(name)
        try:
            await coro_factory()
        except Exception as e:
            health.mark_error(name, str(e))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
        else:
            backoff = base_backoff
```

Wire this into `PynchyApp` for the IPC watcher, message loop, scheduler, and channel connections. Pairs naturally with the health registry above.

---

## 6. Gateway Idempotency + Rate Limiting (Lower Impact, but Easy Win)

Zeroclaw's gateway has per-client sliding-window rate limiting and an idempotency store that deduplicates requests by key with TTL.

**Pynchy today:** The HTTP server has no rate limiting or idempotency. It relies on Tailscale ACLs for access control, which is fine for authentication, but doesn't prevent duplicate deploy webhooks or runaway API calls.

**What to adopt:** A simple idempotency decorator for the `/deploy` and `/api/send` endpoints — store request hashes with a 60-second TTL, return early on duplicates. Sliding-window rate limiting is also straightforward with `aiohttp` middleware. Low effort, prevents a class of operational issues.

---

## Things Zeroclaw Does That Pynchy Already Does Better

To be fair, there are areas where pynchy's architecture is already stronger:

- **Plugin system**: Pynchy's pluggy-based hooks are far more extensible than zeroclaw's compile-time trait implementations. Zeroclaw has no runtime plugin loading.
- **Container isolation**: Pynchy's per-group container model with file-based IPC is more sophisticated than zeroclaw's optional Docker sandboxing.
- **Event bus**: Pynchy's typed `EventBus` is a cleaner internal coordination mechanism than zeroclaw's direct function calls.
- **Queue system**: `GroupQueue` with priority, global concurrency limits, and per-group state is more mature than zeroclaw's single mpsc channel.
- **Multi-channel architecture**: Pynchy's channel plugin system is more extensible than zeroclaw's hardcoded channel implementations.
