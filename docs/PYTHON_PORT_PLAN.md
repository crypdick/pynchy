# Pynchy: Python Port Roadmap

> **What this is:** A design doc and checklist for porting the Pynchy TypeScript codebase to Python. Future agents should treat this as a living guide — check off completed work, update notes, and hand off to the next agent. If something here doesn't make sense when you're actually writing code, trust your judgement. This was written before any Python code existed, so there will be surprises.

---

## The Big Picture

Pynchy is a ~5K-line TypeScript/Node.js app that connects to WhatsApp, routes messages to Claude agents running in isolated Linux containers (Apple Container on macOS), and manages scheduling, IPC, and per-group memory. The Python port should mirror the module structure closely.

**Two separate codebases live in this repo:**
1. **Host process** (`src/`) — the orchestrator that runs on the user's Mac
2. **Agent runner** (`container/agent-runner/`) — runs inside containers, talks to Claude Agent SDK

They communicate via stdin/stdout JSON and file-based IPC. Both are being ported to Python.

---

## Module Map (TypeScript → Python)

| TypeScript Source | Python Target | Purpose | Complexity |
|---|---|---|---|
| `src/types.ts` | `src/pynchy/types.py` | Data models | Low |
| `src/config.ts` | `src/pynchy/config.py` | Env vars, constants, paths | Low |
| `src/logger.ts` | `src/pynchy/logger.py` | Structured logging | Low |
| `src/db.ts` | `src/pynchy/db.py` | SQLite layer (~30 functions) | Medium |
| `src/router.ts` | `src/pynchy/router.py` | Message formatting, XML, routing | Low |
| `src/mount-security.ts` | `src/pynchy/mount_security.py` | Mount validation vs allowlist | Low |
| `src/group-queue.ts` | `src/pynchy/group_queue.py` | Per-group concurrency + global limits | High |
| `src/container-runner.ts` | `src/pynchy/container_runner.py` | Spawn containers, parse streaming output | High |
| `src/ipc.ts` | `src/pynchy/ipc.py` | File-based IPC watcher | Medium |
| `src/task-scheduler.ts` | `src/pynchy/task_scheduler.py` | Cron/interval task execution | Medium |
| `src/channels/whatsapp.ts` | `src/pynchy/channels/whatsapp.py` | WhatsApp via neonize | High |
| `src/whatsapp-auth.ts` | `src/pynchy/auth/whatsapp.py` | WhatsApp QR auth | Medium |
| `src/index.ts` | `src/pynchy/app.py` + `__main__.py` | Main orchestrator | Medium |
| `container/agent-runner/src/index.ts` | `container/agent_runner/src/agent_runner/main.py` | Agent entrypoint (Claude SDK) | High |
| `container/agent-runner/src/ipc-mcp-stdio.ts` | `container/agent_runner/src/agent_runner/ipc_mcp.py` | MCP server for agent IPC | Medium |

## Dependency Map

| Node.js Package | Python Equivalent | Notes |
|---|---|---|
| `@whiskeysockets/baileys` | `neonize` | Go-based (whatsmeow) with Python bindings. API is fundamentally different — not a 1:1 port. |
| `better-sqlite3` | `aiosqlite` | Async wrapper over stdlib `sqlite3`. Needed because sync SQLite blocks asyncio. |
| `cron-parser` | `croniter` | Standard cron parsing. API differs slightly but semantics match. |
| `pino` / `pino-pretty` | `structlog` | Or stdlib `logging`. Structlog is closer to pino's structured JSON style. |
| `qrcode-terminal` | `qrcode` | For WhatsApp auth QR display. |
| `zod` | `pydantic` | Runtime validation. Use sparingly — dataclasses are fine for simple models. |
| `vitest` | `pytest` + `pytest-asyncio` | Plus `pytest-mock` and `freezegun` for time-dependent tests. |
| `@anthropic-ai/claude-agent-sdk` | `claude-code-sdk` | Python agent SDK. Verify API parity before implementing. |
| `@modelcontextprotocol/sdk` | `mcp` | Python MCP SDK package. |

## Concurrency Translation

The whole host process is async. Node.js event loop → Python `asyncio`.

| TypeScript Pattern | Python Equivalent |
|---|---|
| `setInterval(fn, ms)` | `asyncio.create_task` + `asyncio.sleep(s)` loop |
| `setTimeout(fn, ms)` | `asyncio.get_event_loop().call_later(s, fn)` or task + sleep |
| Spawning subprocesses | `asyncio.create_subprocess_exec('container', *args)` |
| `Promise<T>` | `Coroutine` / `await` |
| `new Promise(resolve => ...)` | `asyncio.Future` or `asyncio.Event` |
| Stream `.on('data', cb)` | `async for chunk in proc.stdout` |
| `clearTimeout(timer)` | `task.cancel()` |

Time units: TypeScript uses milliseconds everywhere. Python should use **seconds** (float). Convert at the boundary in `config.py`.

---

## Implementation Phases

Phases are ordered by dependency. Check the box when complete and add a brief note if the implementation diverged from the plan.

### Phase 0: Project Scaffolding
- [x] Create `pyproject.toml` with deps: `neonize`, `aiosqlite`, `croniter`, `structlog`, `qrcode`, `pydantic`, `mcp`
- [x] Create directory structure (see Module Map above)
- [x] Create `__init__.py`, `__main__.py` stubs
- [x] Set up `pytest` with `conftest.py`, `ruff` for linting
- [x] Update `.gitignore` for Python (`.venv/`, `__pycache__/`, `*.pyc`, `dist/`)
- [x] Verify: `uv sync` works, `uv run python -m pynchy` runs and exits cleanly

> Note: Used `hatchling` as build backend for src-layout support. Use `uv sync` for deps, `uv run` to execute.

### Phase 1: Types, Config, Logger
- [x] **types.py** — Port interfaces to dataclasses. Use `Protocol` for `Channel` interface.
- [x] **config.py** — Use `pathlib.Path` for all paths. Store intervals in seconds, not ms.
- [x] **logger.py** — `structlog` singleton. Wire up `sys.excepthook` for uncaught exceptions.
- [x] Port trigger pattern tests from `formatting.test.ts`

> Note: router.py also ported in this phase (was simple and needed for tests). 34 tests passing.

### Phase 2: Database Layer
- [x] **db.py** — All ~30 functions become `async def` using `aiosqlite`. Same schema as TS version.
- [x] Module-level `_db` connection, initialized by `init_database()`, with `_init_test_database()` for tests.
- [x] `aiosqlite.Row` row factory for dict-like access.
- [x] Port all tests from `db.test.ts` using in-memory SQLite.

> Note: 17 DB tests passing. All functions async. JSON migration ported.

### Phase 3: Router & Message Formatting
- [x] **router.py** — `escape_xml()`, `format_messages()`, `strip_internal_tags()`, `format_outbound()`, `route_outbound()`, `find_channel()`
- [x] Port all tests from `formatting.test.ts`

### Phase 4: Mount Security
- [x] **mount_security.py** — Pure logic, no async needed. Use `pathlib` and `os.path.realpath()`.
- [x] `load_mount_allowlist()`, `validate_mount()`, `validate_additional_mounts()`, `generate_allowlist_template()`
- [x] Write tests for allowed/blocked paths, blocked patterns, readonly enforcement.

> Note: Implementation complete. 30 mount security tests written in Phase 12 covering path expansion, blocked patterns, allowed root matching, container path validation, allowlist loading/caching, full mount validation, readonly enforcement, batch validation, and template generation.

### Phase 5: Group Queue
- [x] **group_queue.py** — `GroupQueue` class with asyncio-based concurrency control.
- [x] State per group: `active`, `pending_messages`, `pending_tasks`, `process`, `retry_count`
- [x] Global: `active_count`, `waiting_groups` queue, `MAX_CONCURRENT_CONTAINERS` limit
- [x] Retry with exponential backoff (5s → 10s → 20s... capped at 5 retries)
- [x] `enqueue_message_check()`, `enqueue_task()`, `register_process()`, `send_message()`, `close_stdin()`, `shutdown()`
- [x] Port all tests from `group-queue.test.ts`. Mock `asyncio.sleep` instead of fake timers.

> Note: Key difference from TS: asyncio.ensure_future doesn't run the coroutine synchronously to its first await (unlike JS promises). Fixed by eagerly setting state.active and bumping active_count in the synchronous caller. 8 tests passing.

### Phase 6: Container Runner
- [x] **container_runner.py** — `run_container_agent()` as the main entry point.
- [x] Mount building: `_build_volume_mounts()` with `Path.mkdir(parents=True, exist_ok=True)`
- [x] Process spawn: `asyncio.create_subprocess_exec` with `stdin=PIPE, stdout=PIPE, stderr=PIPE`
- [x] Streaming stdout parser: accumulate buffer, extract between `OUTPUT_START` / `OUTPUT_END` sentinel markers
- [x] Timeout: `loop.call_later()` with activity-based reset. Timeout after output = success (idle cleanup). Timeout before output = error.
- [x] Skills sync, env file writing, log file writing
- [x] `write_tasks_snapshot()`, `write_groups_snapshot()`
- [x] Port tests from `container-runner.test.ts`. FakeProcess with asyncio.StreamReader.

> Note: 22 container runner tests. Used `asyncio.StreamReader.feed_data()/feed_eof()` for FakeProcess instead of Node PassThrough. Timeout tests use negative IDLE_TIMEOUT values to avoid 30s grace period wait. Task scheduler stub wired to real `run_container_agent()`.

### Phase 7: IPC Watcher
- [x] **ipc.py** — `start_ipc_watcher()` as async polling loop (1s interval).
- [x] `process_task_ipc()` with `match/case` for command dispatch (Python 3.10+).
- [x] Authorization: non-main groups can only operate on themselves.
- [x] Atomic file processing: read → execute → delete. Move failures to `errors/` dir.
- [x] Port all tests from `ipc-auth.test.ts` (largest test file — 594 lines).

> Note: 32 IPC auth tests passing. All authorization patterns, schedule types, and context_mode logic ported.

### Phase 8: Task Scheduler
- [x] **task_scheduler.py** — `start_scheduler_loop()` as async polling loop (60s interval).
- [x] `croniter` for cron, simple arithmetic for interval, `datetime.fromisoformat()` for once.
- [x] Context modes: `group` (shared session) vs `isolated` (fresh session).
- [x] `update_task_after_run()`: calculate next_run, log to `task_run_logs`.

> Note: Scheduler loop and _run_task implemented. Container runner integration is a stub pending Phase 6.

### Phase 9: WhatsApp Channel (neonize)
- [x] **channels/whatsapp.py** — `WhatsAppChannel` class implementing `Channel` protocol.
- [x] **auth/whatsapp.py** — QR code auth flow using neonize.
- [x] Message receive: filter own messages, store to DB, notify metadata for discovery.
- [x] Message send: queue when disconnected, flush on reconnect.
- [x] Group sync: fetch metadata periodically (24h cache).
- [x] Reconnection: handle disconnects gracefully.

> Note: neonize's API differs substantially from baileys. Key differences: auth is SQLite-based (not file-based), reconnection is handled internally by whatsmeow (no manual retry logic needed), JIDs are protobuf objects (Jid2String() for conversion), events are separate typed classes. Uses NewAClient for asyncio integration. ChatPresence enum names are prefixed (CHAT_PRESENCE_COMPOSING, not COMPOSING). is_logged_in is async on NewAClient.

### Phase 10: Main Orchestrator
- [x] **app.py** — `PynchyApp` class (replaces module-level globals from `index.ts`).
- [x] State: `last_timestamp`, `sessions`, `registered_groups`, `last_agent_timestamp`, `queue`, `whatsapp`
- [x] `run()`: init DB → load state → connect WhatsApp → start scheduler + IPC + message loop
- [x] Message loop (2s poll): fetch new messages → check triggers → dispatch to queue
- [x] `process_group_messages()`: format XML → advance cursor → run container → rollback on error before output
- [x] Crash recovery: `recover_pending_messages()` on startup
- [x] Shutdown: SIGTERM/SIGINT → `queue.shutdown()` → `whatsapp.disconnect()` → exit
- [x] `ensure_container_system_running()`: check Apple Container, kill orphans
- [x] **`__main__.py`**: `asyncio.run(PynchyApp().run())`
- [x] Port tests from `routing.test.ts`

> Note: PynchyApp class ports all logic from index.ts into instance state. Dependency adapters (inner classes) wire scheduler and IPC deps. IPC deps required async `get_available_groups()` fix. 8 routing tests + 12 integration tests passing.

### Phase 11: Container Agent Runner (Python)
> This phase can be developed **in parallel** with Phases 2-10.

- [x] **container/agent_runner/** — Separate Python package with its own `pyproject.toml`
- [x] **main.py** — Read JSON from stdin, run `ClaudeSDKClient` via Python Claude Agent SDK (`claude-agent-sdk`), write sentinel-wrapped JSON to stdout.
- [x] `MessageStream`: async generator using `asyncio.Queue` (replaces the TS `AsyncIterable` class).
- [x] IPC polling during query: drain `/workspace/ipc/input/*.json`, detect `_close` sentinel.
- [x] Pre-compact hook: archive transcript to `conversations/` as markdown.
- [x] Session resume: pass `session_id` and `resume_at` to SDK.
- [x] **ipc_mcp.py** — MCP server using Python `mcp` package. Tools: `send_message`, `schedule_task`, `list_tasks`, `pause_task`, `resume_task`, `cancel_task`, `register_group`.
- [x] Atomic IPC file writes: temp file → `os.rename()`.
- [x] **Dockerfile** — Python 3.13-slim base. Still needs Node.js for `agent-browser` (dual-runtime container).
- [x] **build.sh** — Update for Python container (image: `pynchy-agent`).

> Note: Uses `ClaudeSDKClient` (not `query()`) for session continuity and hooks support. Sequential `client.query()` calls with session resume replace the TS MessageStream in-query piping pattern. camelCase→snake_case boundary removed since both host and container are now Python. Container user changed from `node` to `agent`. All 22 container runner tests updated for snake_case.

### Phase 12: Integration Testing & Polish
- [x] End-to-end: start app → mock WhatsApp → send message → verify container spawns → output returns
- [x] IPC round-trip: covered by existing ipc_auth tests (32 tests)
- [x] Scheduler: covered by existing scheduler tests
- [x] Multi-group concurrency: covered by existing group_queue tests (8 tests)
- [x] Graceful shutdown: tested via state persistence round-trip tests
- [x] Crash recovery: tested via `recover_pending_messages()` integration tests
- [x] Update CLAUDE.md for Python commands (`uv run python -m pynchy`, `uv run pytest`)
- [x] Mount security tests: 30 tests covering allowlist, blocked patterns, readonly enforcement
- [ ] Update launchd plist for Python entrypoint

> Note: 172 total tests passing. Integration tests cover message processing pipeline (trigger detection, container spawn, output routing, cursor rollback on error), state persistence round-trips, and crash recovery. Mount security tests fill the gap deferred from Phase 4. Launchd plist update is a deployment-time task.

---

## Critical Paths & Parallelism

```
Phase 0 → 1 → 2 → 5 → 6 → 8 → 10 → 12
              ├→ 3 → 7 ──────────↗
              ├→ 4 ───────────────↗
              └→ 9 ───────────────↗

Phase 11 (container agent runner) is independent — build in parallel with everything.
```

**Critical path:** 0 → 1 → 2 → 5 → 6 → 10 → 12

---

## Known Risks

| Risk | Severity | Mitigation |
|---|---|---|
| **neonize API mismatch** — fundamentally different from baileys | High | Build narrow adapter. Accept this module won't be 1:1. Research LID handling and reconnection early. |
| **Container runner streaming** — asyncio subprocess buffering differs from Node streams | High | Port tests first. Use `asyncio.StreamReader` carefully. Test all 3 timeout scenarios (no output, mid-output, post-output). |
| **Group queue state machine** — intricate concurrency with retries and drain cascading | Medium | Port tests first (TDD). Single-threaded asyncio shouldn't need locks if `await` points are managed. |
| **Claude Agent SDK Python** — different package, potentially different API surface | Medium | Build a minimal test script calling `query()` with all options before attempting full port. Check: resume, hooks, MCP server config, permission mode. |
| **aiosqlite performance** — adds thread overhead vs sync better-sqlite3 | Low | Operations are small. Won't matter in practice. |

---

## Out of Scope / Future Roadmap

These are ideas that are explicitly **not part of the Python port** but worth tracking for later.

- **Agent-side async task dispatch.** Agents running inside containers should be able to fire off tasks in separate sandboxed environments (e.g., a browser task, a code execution job) and get results back asynchronously. Think of it as a skill that lets the agent say "run this in a fresh sandbox and give me the result." This could be built as an MCP tool available to the agent runner, backed by something like OpenSandbox, Docker, or a lightweight job queue. The Pynchy host harness itself should keep using containers directly — this is about giving agents *inside* those containers the ability to fan out work.


---

## Notes for Future Agents

1. **Read the design docs first.** Before writing any code, read all the docs in `docs/`: `REQUIREMENTS.md` (philosophy and architecture decisions), `SPEC.md` (full technical specification), `SDK_DEEP_DIVE.md` (how the Claude Agent SDK works), `SECURITY.md` (trust model and security boundaries), and `DEBUG_CHECKLIST.md` (known issues). Skip `SECURITY_HARDENING_PLAN.md` — that's a post-port follow-up project, not part of the Python port. These docs define the spirit of the codebase. If your code doesn't match the philosophy described in `REQUIREMENTS.md`, it's wrong even if the tests pass.

2. **Read the TypeScript before porting.** Before porting any module, read the original TS file completely. The code is well-written and the patterns are intentional.

3. **Port tests alongside code.** Each phase has corresponding test files. Don't skip them — they catch real bugs and serve as the validation gate before moving on.

4. **Keep this doc updated.** Check off phases as you complete them. If you diverge from the plan, add a brief note so the next agent knows what happened. Don't add verbose implementation details — keep it scannable.

5. **The WhatsApp channel is the wild card.** neonize is the best Python option but it's not baileys. Expect to spend time reading neonize source code and examples. The `Channel` protocol interface is your safety net — as long as `WhatsAppChannel` implements it correctly, the rest of the system doesn't care how it works internally.

6. **The container agent runner is a separate world.** It runs inside a Linux VM, has its own dependencies, and communicates only via stdin/stdout JSON and IPC files. You can develop and test it independently. The sentinel markers (`---PYNCHY_OUTPUT_START---` / `---PYNCHY_OUTPUT_END---`) are the contract between host and container.

7. **Don't over-abstract.** The TypeScript codebase is deliberately simple. Resist the urge to add extra layers, base classes, or frameworks. If the TS version does something in 20 lines, the Python should too.

8. **asyncio is the concurrency model.** Every polling loop, every subprocess interaction, every DB call goes through asyncio. There are no threads except inside aiosqlite (which handles that for you).

9. **Time units: seconds, not milliseconds.** The TS uses ms everywhere. Python should use seconds (float). Convert once in `config.py` and never think about it again.

10. **Trust your judgement.** This roadmap was written before any Python code existed. If something doesn't make sense when you're implementing it, adapt. The goal is a working Python port, not blind adherence to this document.
