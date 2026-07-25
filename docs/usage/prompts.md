# Prompts

Prompts customize the instructions agents receive in their system prompt without editing code.

## What Prompts Are

Prompts are markdown files injected into an agent's system prompt. They contain behavioral instructions, persona definitions, tool usage guidance, and anything else that shapes how the agent operates. Different workspaces can receive different prompt sets through their profiles, so admin agents and regular group agents can get different instructions.

## Convention-Based Resolution

Prompt names map to files by convention. Pynchy uses the personalized file
when it exists; otherwise it uses the public default:

```text
"base"           ->  data/personalization/prompts/base.md
                  ->  data/defaults/prompts/base.md
"idle-escape"    ->  data/personalization/prompts/idle-escape.md
                  ->  data/defaults/prompts/idle-escape.md
```

No registry or config mapping exists. The name identifies the file. Place a
deployment-specific prompt at `data/personalization/prompts/<name>.md` and
reference it by name in your config.

## Assigning Prompts

Prompts are assigned through reusable workspace profiles in
`data/personalization/pynchy.toml`.

```toml
[profiles.pynchy-dev]
prompts = ["base", "idle-escape", "pynchy-code-improver"]

[workspaces.my-agent]
profiles = ["pynchy-dev"]
```

### How Profiles Merge

When a workspace lists multiple profiles, prompt lists are unioned with order-preserved dedup. Given this config:

```toml
[profiles.base]
prompts = ["base", "idle-escape"]

[profiles.ops]
prompts = ["base", "pynchy-admin-ops"]

[workspaces.admin]
profiles = ["base", "ops"]
```

The `admin` workspace receives `["base", "idle-escape", "pynchy-admin-ops"]`. First occurrence wins on duplicates.

## File Location and Format

Public defaults live under `data/defaults/prompts/`; deployment-specific prompts
live under `data/personalization/prompts/`. Plain markdown works best. Multiple
matching prompts are concatenated with `---` separators.

Files ending in `.EXAMPLE` are ignored because they are repo templates.

## Relationship to Project Instructions

Prompts provide core-independent instructions. Keep project-specific agent
instructions in the instruction file your selected core understands (for
example, `CLAUDE.md` for Claude Code or `AGENTS.md` for Codex) and use Pynchy
prompts for workspace persona, communication style, and operational
procedures. A repo-backed workspace gives the core that repository as its
working directory, so its project instructions remain available alongside the
configured prompts.

## KV Cache Considerations

Prompt content stays stable across session resumes. The system prompt does not change between runs, so the API's KV cache is preserved. Do not put ephemeral or frequently changing content in prompts. Use system notices for per-run context.
