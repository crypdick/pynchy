# Prompts

Prompts add workspace-specific context without changing code. Prefer a short
contract: what the agent should accomplish, what authority it has, and what
success looks like.

Don't prescribe investigation order, planning rituals, tool loops, or obvious
reasoning steps. The runtime enforces security and publication boundaries; the
agent owns its method. Add instructions only for constraints the host can't
enforce or context the agent can't reasonably infer.

## Resolve prompt files

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

## Assign prompts

Prompts are assigned through reusable workspace profiles in
`data/personalization/pynchy.toml`.

```toml
[profiles.pynchy-dev]
prompts = ["base", "idle-escape", "pynchy-code-improver"]

[workspaces.my-agent]
profiles = ["pynchy-dev"]
```

### Merge profiles

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

## Choose a location and format

Public defaults live under `data/defaults/prompts/`. Deployment-specific
prompts live under `data/personalization/prompts/`. Repository upgrades don't
overwrite personalized prompts. Plain Markdown works best. Multiple matching
prompts are concatenated with `---` separators.

Files ending in `.EXAMPLE` are ignored because they are repo templates.

## Relate prompts to project instructions

Prompts provide core-independent instructions. Keep project-specific context in
the instruction file your selected core understands, such as `CLAUDE.md` or
`AGENTS.md`. Use Pynchy prompts for workspace purpose, authority, and
deployment-specific facts. A repo-backed workspace exposes both.

## Preserve prompt caching

Prompt content stays stable across session resumes. Don't include ephemeral or
frequently changing context. Use system notices for per-run information.

When migrating older personalized prompts, remove sentinel commits and fixed
plan, validation, sync, or completion sequences. Scheduled runs finish when the
agent returns its final result; the host closes the run. When the task produces
code, the agent explicitly publishes committed changes as a pull request.
