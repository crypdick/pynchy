# Directives

Directives are how you customize what instructions your agents receive in their system prompt — without editing code.

## What Directives Are

Directives are markdown files injected into an agent's system prompt. They contain behavioral instructions, persona definitions, tool usage guidance, and anything else that shapes how the agent operates. Different workspaces can receive different sets of directives, so admin agents and regular group agents can get different instructions.

## Convention-Based Resolution

Directive names map to files by convention:

```
"base"           →  directives/base.md
"idle-escape"    →  directives/idle-escape.md
"pynchy-dev"     →  directives/pynchy-dev.md
```

No registry or config mapping — the name **is** the path. Place your directive file at `directives/<name>.md` and reference it by name in your config.

## Assigning Directives

Directives are assigned through workspace config tiers in `config.toml`. Three tiers, with lists **unioned** across all of them:

### 1. `[universal]` — applies to every workspace

```toml
[universal]
directives = ["base", "idle-escape"]
```

### 2. `[profiles.*]` — reusable config bundles

```toml
[profiles.pynchy-dev]
directives = ["pynchy-admin-ops", "pynchy-code-improver"]
```

### 3. `[workspaces.*]` — specific to one workspace

```toml
[workspaces.my-agent]
profile = "pynchy-dev"
directives = ["extra-safety"]
```

### How Tiers Merge

The three lists are unioned with order-preserved dedup. Given the config above, a workspace using the `pynchy-dev` profile with its own `["extra-safety"]` directive gets:

```
["base", "idle-escape", "pynchy-admin-ops", "pynchy-code-improver", "extra-safety"]
```

Universal first, then profile, then workspace. First occurrence wins on duplicates.

## File Location and Format

Directive files live under `directives/` in the project root. Plain markdown — write them like a CLAUDE.md file. Multiple matching directives are concatenated with `---` separators.

Files ending in `.EXAMPLE` are ignored (they're repo templates).

## Relationship to CLAUDE.md

Directives stack on top of the project's `CLAUDE.md`. Admin and repo_access workspaces set `cwd` to `/workspace/project`, so Claude Code finds the project-root `CLAUDE.md` natively. Directives carry the stuff that doesn't belong in the project CLAUDE.md — persona, communication style, operational procedures.

## KV Cache Considerations

Directive content stays stable across session resumes. The system prompt doesn't change between runs, so the API's KV cache is preserved. Don't put ephemeral or frequently-changing content in directives — use system notices for per-run context.
