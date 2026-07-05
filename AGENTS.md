# Pynchy

Personal Claude assistant. See [README.md](README.md) for philosophy. See [installation guide](https://pynchy.ricardodecal.com/install/) for installation. See [architecture](https://pynchy.ricardodecal.com/architecture/) for architecture. See [CONVENTIONS.md](CONVENTIONS.md) for design principles (composition over inheritance, parse-don't-validate, semantic types, code/doc coupling) — apply them when writing or reviewing code.

## Deployment Awareness

You are usually NOT running on the production host. The pynchy service runs on `pynchy-server` (reachable via Tailscale SSH). Before making changes that affect the live service (config.toml, server-side files, service restarts), read the [pynchy-ops skill](.claude/skills/pynchy-ops/SKILL.md) for deployment procedures, auto-deploy behavior, and how to observe the running service.

## Quick Context

Python process that connects to messaging channels (WhatsApp, Slack, etc. via plugins), routes messages to Claude Agent SDK running in containers (Apple Container on macOS, Docker on Linux). Each group has isolated filesystem and memory.

## Key Files

Where code lives. For how it works, see the [architecture overview](https://pynchy.ricardodecal.com/architecture/).

| File | Purpose |
|------|---------|
| `src/pynchy/state/` | SQLite operations (async, aiosqlite) — package with domain submodules |
| `src/pynchy/host/container_manager/ipc/` | IPC watcher, registry-based dispatch, service handlers |
| `src/pynchy/host/git_ops/` | Git sync, worktrees, and shared helpers |
| `src/pynchy/host/orchestrator/messaging/` | Message pipeline — inbound routing, processing, outbound delivery |
| `src/pynchy/host/orchestrator/` | App lifecycle, agent execution, scheduling, workspace config |
| `src/pynchy/plugins/runtimes/` | Runtime detection, platform providers, system checks |
| `src/pynchy/plugins/` | Plugin system — registry, hookspecs, channels, agent cores, integrations |
| `src/pynchy/host/container_manager/` | Container orchestration — mounts, credentials, process management |
| `src/pynchy/host/container_manager/mcp/` | MCP lifecycle — LiteLLM sync, Docker on-demand, team provisioning |
| `src/pynchy/host/container_manager/security/` | Security policy middleware and audit logging |
| `src/pynchy/config/` | Pydantic BaseSettings config (TOML + env overrides), MCP config, directives |
| `src/pynchy/config/mcp.py` | MCP server config models (`McpServerConfig`) |
| `src/pynchy/host/orchestrator/concurrency.py` | Per-group queue with global concurrency limit |
| `src/pynchy/host/orchestrator/task_scheduler.py` | Runs scheduled tasks |
| `src/pynchy/config/directives.py` | Scoped system prompt directive resolution |
| `src/pynchy/types.py` | Data models (dataclasses) |
| `src/pynchy/logger.py` | Structured logging (structlog) |
| `src/pynchy/agent/` | Container-side code — skills, agent runner, build scripts |
| `directives/` | System prompt directive markdown files |
| `groups/{name}/` | Per-group workspace files (isolated) |
| `src/pynchy/agent/skills/` | Agent skills with YAML frontmatter (tier, name, description) |
| `backlog/TODO.md` | Work item index — one-line items linking to plan files in status folders |

## Detailed Guides

| Guide | When to Read |
|-------|-------------|
| [Architecture](https://pynchy.ricardodecal.com/architecture/) | System design, container isolation, message routing, groups, tasks |
| [Security model](https://pynchy.ricardodecal.com/architecture/security/) | Trust model, security boundaries, credential handling |
| [Plugin authoring](https://pynchy.ricardodecal.com/plugins/) | Writing plugins: hooks, packaging, distribution |
| [Worktree isolation](https://pynchy.ricardodecal.com/usage/worktrees/) | How non-admin groups get isolated git worktrees |
| [Style guide](https://pynchy.ricardodecal.com/contributing/contributing-docs/) | Documentation philosophy, information architecture, code comments |

## Expert Pushback Policy

Treat the user as a peer, not someone to serve: push back directly on inelegant or unsound proposals, advocate for the right solution, and only yield on an explicit "I insist". The full protocol and worked example live in [`directives/base.md`](directives/base.md#expert-pushback-policy).

## Python & Tool Usage

Use `uv run python` (never bare `python`/`python3`) and `uvx` for CLI tools (`uvx ruff`, `uvx pytest`) — never `pip install` a tool globally. See the [pynchy-dev skill](.claude/skills/pynchy-dev/SKILL.md) for the full command set and development workflow.

## Diligence and curiousity

When you notice unexpected or fishy-looking code, make sure to document it. For example, `TODO: this function is hard-coded to returns an empty list, but in the other implementation it doesn't. Investigate why this is`. It's ok if you don't plan to solve it yourself, just make sure not to lose the insight-- false positives are better than false negatives. If you find the answer, make sure to circle back and document it. To continue this example: `Returns an empty list because this method is meaningless for this subclass`. That way, it doesn't confuse you next time you encounter the suspicuous code.

If you ever find yourself tracing a function to figure out these gotchas, make sure to document your learnings in code comments for the benefit of future maintainers so that they don't have to relearn the gotchas. Or, add a TODO for yourself to improve janky code some other day.
