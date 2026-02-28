# Directives

This page explains how to configure system prompt directives — the mechanism that controls what instructions your agents receive. Understanding directives helps you customize agent behavior without editing code.

## What Directives Are

Directives are markdown files that get injected into an agent's system prompt. They contain behavioral instructions, persona definitions, tool usage guidance, and anything else that shapes how the agent operates. Different sandboxes can receive different sets of directives, so you can give admin agents different instructions than regular group agents.

## Convention-Based Resolution

Directive names map to files by convention:

```
"base"           →  directives/base.md
"idle-escape"    →  directives/idle-escape.md
"pynchy-dev"     →  directives/pynchy-dev.md
```

There is no registry or config mapping — the name **is** the path. Place your directive file at `directives/<name>.md` and reference it by name in your config.

## Assigning Directives

Directives are assigned through sandbox config tiers in `config.toml`. There are three tiers, and directive lists are **unioned** across all of them:

### 1. `[sandbox_universal]` — applies to every sandbox

```toml
[sandbox_universal]
directives = ["base", "idle-escape"]
```

### 2. `[sandbox_profiles.*]` — reusable config bundles

```toml
[sandbox_profiles.pynchy-dev]
directives = ["pynchy-admin-ops", "pynchy-code-improver"]
```

### 3. Per-sandbox — specific to one sandbox

```toml
[workspaces.my-agent]
profile = "pynchy-dev"
directives = ["extra-safety"]
```

### How Tiers Merge

Directive lists are **unioned** across all three tiers with order-preserved deduplication. Given the config above, a sandbox using the `pynchy-dev` profile with its own `["extra-safety"]` directive receives:

```
["base", "idle-escape", "pynchy-admin-ops", "pynchy-code-improver", "extra-safety"]
```

Universal directives come first, then profile directives, then per-sandbox directives. Duplicates are removed (first occurrence wins).

## File Location and Format

Directive files live under `directives/` in the project root. They are plain markdown — write them the same way you would write a CLAUDE.md file. Multiple matching directives are concatenated with `---` separators.

Files ending in `.EXAMPLE` are automatically ignored (they exist as templates in the repo).

## Relationship to CLAUDE.md

Directives are additive to the project's `CLAUDE.md`. Admin and repo_access sandboxes have their `cwd` set to `/workspace/project`, so Claude Code discovers the project-root `CLAUDE.md` natively. Directives provide additional instructions on top of that — things like persona, communication style, and operational procedures that are not appropriate for the project CLAUDE.md.

## KV Cache Considerations

Directive content is stable across session resumes — it does not change between runs. This means the system prompt stays constant, preserving the API's KV cache. Avoid putting ephemeral or frequently-changing content in directives; use system notices for per-run context instead.
