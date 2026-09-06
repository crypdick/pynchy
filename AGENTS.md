# Pynchy

Personal Claude assistant. See [README.md](README.md) for philosophy. See [installation guide](docs/installation/index.md) for installation. See [architecture](docs/architecture/index.md) for architecture. See [CONVENTIONS.md](CONVENTIONS.md) for design principles (composition over inheritance, parse-don't-validate, semantic types, code/doc coupling) — apply them when writing or reviewing code.

## Architectural Direction

Pynchy's architectural ambition is a modular monolith with semantic domain contracts, use-case-owned ports, concrete adapters, and explicit composition roots—not a distributed-services or dependency-injection-framework rewrite. Before changing a cross-subsystem dependency, read [architecture.toml](architecture.toml), the executable source of truth for current package ownership, public surfaces, and allowed role dependencies.

Burn down `architecture-baseline.toml` package by package. For each multi-module
package with cross-package consumers, migrate its sole public surface to
`<package>.api`; a façade made entirely of curated re-exports is valid. Move all
consumers in the package pass, update `public_modules`, and remove every stale
baseline entry. Correct package roles when direction is wrong. Do not weaken the
policy or regenerate the baseline merely to reduce the count.

## Deployment Awareness

You are usually NOT running on the production host. The live host is deployment-specific and should come from local memory, environment, or the operator, not from public repo defaults. Before making changes that affect the live service (config.toml, server-side files, service restarts), read the [pynchy-ops skill](.claude/skills/pynchy-ops/SKILL.md) for deployment procedures, auto-deploy behavior, and how to observe the running service.

## Quick Context

Python process that connects to messaging channels (WhatsApp, Slack, etc. via plugins), routes messages to Claude Agent SDK running in containers (Apple Container on macOS, Docker on Linux). Each group has isolated filesystem and memory.

## Key Files

Where code lives. For how it works, see the [architecture overview](docs/architecture/index.md).

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
| `src/pynchy/config/` | Pydantic BaseSettings config (TOML + env overrides), MCP config, prompts |
| `src/pynchy/plugins/mcp_server.py` | Validated MCP server templates (`McpServerConfig`) |
| `src/pynchy/host/orchestrator/concurrency.py` | Per-group queue with global concurrency limit |
| `src/pynchy/host/orchestrator/task_scheduler.py` | Runs scheduled tasks |
| `src/pynchy/config/prompts.py` | Scoped system prompt resolution |
| `src/pynchy/types.py` | Data models (dataclasses) |
| `src/pynchy/logger.py` | Structured logging (structlog) |
| `src/pynchy/agent/` | Container-side code — skills, agent runner, build scripts |
| `data/defaults/prompts/` | Public system prompt markdown files |
| `groups/{name}/` | Per-group workspace files (isolated) |
| `src/pynchy/plugins/**/skills/` | Tool-associated skills with YAML frontmatter (tier, name, description) |
| [Linear task tracking](docs/integrations/linear.md) | Canonical repository work items, authorization, and results |

## Detailed Guides

| Guide | When to Read |
|-------|-------------|
| [Architecture](docs/architecture/index.md) | System design, container isolation, message routing, groups, tasks |
| [Architecture policy](architecture.toml) | Package ownership, public surfaces, and allowed role dependencies |
| [Security model](docs/architecture/security.md) | Trust model, security boundaries, credential handling |
| [Plugin authoring](docs/plugins/index.md) | Writing plugins: hooks, packaging, distribution |
| [Worktree isolation](docs/usage/worktrees.md) | How non-admin groups get isolated git worktrees |
| [Style guide](docs/contributing/contributing-docs.md) | Documentation philosophy, information architecture, code comments |

## Expert Pushback Policy

Treat the user as a peer, not someone to serve: push back directly on inelegant or unsound proposals, advocate for the right solution, and only yield on an explicit "I insist". Follow the full protocol in your global agent instructions.

## Python & Tool Usage

Use `uv run python` (never bare `python`/`python3`) and `uvx` for CLI tools (`uvx ruff`, `uvx pytest`) — never `pip install` a tool globally. See the [pynchy-dev skill](.claude/skills/pynchy-dev/SKILL.md) for the full command set and development workflow.

## Prek Hooks

`prek.toml` runs custom lint checks (banned `print()`, broad exception handling, file-length budget, dead code, dependency integrity, complexity, temporal language in `src/` comments, and tests crossing private first-party implementation boundaries) plus strict mypy type checking — all blocking. The private-boundary check covers imports, private modules, and known first-party attributes across `pynchy`, `agent_runner`, and first-party `scripts`. It does not inspect dotted patch targets: those substitute collaborators while a test drives public behavior, and are not themselves private-shape assertions.

Exempt a specific line from a *blocking* custom check with `# allow: <hook-id>`, e.g. `# allow: print-statements` or `# allow: exception-handling`. The private-boundary checker is narrower: use `# allow: private-test-imports - external-process: <why no public observable exists>` only for an unavoidable external-process side channel. Always justify the exemption inline and prefer fixing the underlying issue over exempting it.

## Diligence and curiousity

When you notice unexpected or fishy-looking code, make sure to document it. For example, `TODO: this function is hard-coded to returns an empty list, but in the other implementation it doesn't. Investigate why this is`. It's ok if you don't plan to solve it yourself, just make sure not to lose the insight-- false positives are better than false negatives. If you find the answer, make sure to circle back and document it. To continue this example: `Returns an empty list because this method is meaningless for this subclass`. That way, it doesn't confuse you next time you encounter the suspicuous code.

If you ever find yourself tracing a function to figure out these gotchas, make sure to document your learnings in code comments for the benefit of future maintainers so that they don't have to relearn the gotchas. Or, add a TODO for yourself to improve janky code some other day.
