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

## Detailed Guides

| Guide | When to Read |
|-------|-------------|
| [Architecture](docs/architecture/index.md) | System design, container isolation, message routing, groups, tasks |
| [Architecture policy](architecture.toml) | Package ownership, public surfaces, and allowed role dependencies |
| [Security model](docs/architecture/security.md) | Trust model, security boundaries, credential handling |
| [Plugin authoring](docs/plugins/index.md) | Writing plugins: hooks, packaging, distribution |
| [Worktree isolation](docs/usage/worktrees.md) | How non-admin groups get isolated git worktrees |
| [Style guide](docs/contributing/contributing-docs.md) | Documentation philosophy, information architecture, code comments |

## Instruction ownership

Global agent instructions own interaction preferences. This file owns repository
invariants; `CONVENTIONS.md` owns design principles; skills own task procedures.
Executable checks live in `pyproject.toml`, `prek.toml`, and `architecture.toml`.
Link to the owner instead of copying its rules. Resolve conflicts using instruction
priority and the user's current authorization; repair verified stale references.

## Python & Tool Usage

Use `uv run python` (never bare `python`/`python3`) and `uvx` for CLI tools (`uvx ruff`, `uvx pytest`) — never `pip install` a tool globally. See the [pynchy-dev skill](.claude/skills/pynchy-dev/SKILL.md) for the full command set and development workflow.

## Prek Hooks

`prek.toml` runs custom lint checks (banned `print()`, broad exception handling, file-length budget, dead code, dependency integrity, complexity, and tests crossing private first-party implementation boundaries) plus strict mypy type checking — all blocking. The temporal-comment check is advisory; use judgment about domain terms. The private-boundary check covers imports, private modules, and known first-party attributes across `pynchy`, `agent_runner`, and first-party `scripts`. It does not inspect dotted patch targets: those substitute collaborators while a test drives public behavior, and are not themselves private-shape assertions.

Exempt a specific line from a *blocking* custom check with `# allow: <hook-id>`, e.g. `# allow: print-statements` or `# allow: exception-handling`. The private-boundary checker is narrower: use `# allow: private-test-imports - external-process: <why no public observable exists>` only for an unavoidable external-process side channel. Always justify the exemption inline and prefer fixing the underlying issue over exempting it.

## Non-obvious behavior

Document verified, surprising constraints where future maintainers need them.
Investigate suspicious behavior before stating a cause. Track unresolved defects
through [Linear](docs/integrations/linear.md); avoid speculative TODOs and duplicate
notes for facts already clear from code or tests.
