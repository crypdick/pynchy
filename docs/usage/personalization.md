# Personalization repository

Keep deployment-specific Pynchy configuration in a separate, usually private
Git repository. Check it out at the conventional path:

```text
pynchy/
├── data/
│   ├── defaults/          # public, tracked Pynchy baseline
│   └── personalization/   # independent repository, ignored by Pynchy
└── .env                   # deployment secrets only
```

Pynchy does not clone the repository or pull remote-only changes. It validates,
commits, and pushes valid local changes from the host sync loop. Before a push,
the host fetches and rebases local commits onto the remote branch.

## Create the repository

Start from the shipped example:

```bash
cp -R config-examples/personalization /tmp/pynchy-personalization
cd /tmp/pynchy-personalization
git init
git add .
git commit -m "Initialize Pynchy personalization"
```

Push that directory to a private remote, then check it out inside each Pynchy
installation:

```bash
git clone git@github.com:YOUR-ACCOUNT/pynchy-personalization.git \
  data/personalization
```

Do not use a Git submodule. `data/personalization/` is ignored by the public
Pynchy repository, so the nested checkout remains independent and Pynchy does
not publish its URL or commit identity.

## Directory contract

```text
data/personalization/
├── pynchy.toml
├── litellm.yaml
├── automations/
│   └── weekly-review.toml
├── workspaces/
│   └── admin.toml
├── pipelines/
│   └── research.toml
├── prompts/
│   ├── souls/
│   ├── executors/
│   └── reviewers/
└── skills/
    └── my-skill/
        └── SKILL.md
```

`pynchy.toml` and `litellm.yaml` are required. The other directories are
optional. Pynchy refuses normal service startup when the required files are
missing or the tree does not validate.

Configuration resolves in this order, from lowest to highest priority:

1. `data/defaults/pynchy.toml`
2. `data/personalization/pynchy.toml`
3. `.env` and process environment variables

Nested TOML mappings merge recursively. Lists and scalar values replace the
lower layer. `gateway.litellm_config` is convention-owned: Pynchy wires it to
`data/personalization/litellm.yaml`, so do not set that field yourself.

Keep API keys, tokens, and passwords in the root `.env`, not in the
personalization repository. Environment overrides use `__` between nested
fields, such as `GATEWAY__MASTER_KEY`.

## Apply configuration changes

The host sync loop validates the complete candidate before applying any
personalized configuration change. Pynchy then uses the strongest mechanism
required by the changed fields:

| Change | Application |
|---|---|
| Profile `skills` or `denied_skills`; learning review limits; container query timeout | Refresh before the next turn |
| Selected souls, pipelines, prompt files, profile model, repositories, execution mode, working directory, or security policy | Pause affected workspace queues, replace their sessions, then resume queued work |
| Global reasoning effort, learning vault mounts, blocked mount patterns, or container image, memory, and idle timeout | Replace every registered workspace session |
| Tools, admin status, profile composition or identity, [workspace topology](../superpowers/future-work/workspace-topology-hot-reconciliation.md), connections, plugins, repositories, queue policy, secrets, and other host infrastructure | Restart the host |

A mixed edit uses the strongest class and never partially publishes weaker
changes. Session replacement preserves messages, conversation-control state,
queued work, and sticky security taint. Unaffected workspace queues continue
running. Changes to `.env` remain restart-sensitive.

## Automations

Each direct `automations/*.toml` file declares one automation. Its filename
without `.toml` is the stable job ID:

```toml
# data/personalization/automations/weekly-review.toml
schema_version = 1

[job]
schedule = "0 9 * * 1"
workspace = "admin"
prompt = """
Review the previous week, summarize the important outcomes, and identify the
three highest-leverage priorities for the coming week.
"""
display_name = "Weekly review"
```

Agent jobs require an inline `prompt`. Host and deterministic jobs reject it.
A personalized automation replaces a same-named public default automation as
one unit. Do not also declare the same ID under `[jobs]` in `pynchy.toml`.

Runtime schedule state and run evidence remain in SQLite and Temporal. The
automation file is the desired-state declaration, not an execution log.

## Skills

Pynchy copies skills from these sources in order:

1. Public defaults in `data/defaults/skills/`
2. Personalized skills in `data/personalization/skills/`
3. Skills contributed by enabled plugins

A personalized skill can replace a same-named public default or plugin skill,
so an agent can preserve an improvement without editing an installed package.
Plugin-to-plugin name collisions still fail session preparation. Each skill
directory must contain a `SKILL.md` whose frontmatter has
matching `name` and non-empty `description` fields. An optional `tier` must be
non-empty; skills without one use the `community` tier.

Treat skill directories under `data/sessions/` as generated runtime registries.
Pynchy gives every agent read-write access to the canonical personalized skill
registry through `$PYNCHY_SKILLS_ROOT`. Agents may create or improve skills
there. Pynchy refreshes selected skills into each generated session registry
before the next turn, without restarting the service. Valid personalization
changes are committed and pushed automatically.

Do not author durable changes in a session's `.claude/skills/` or
`.codex/skills/` directory. Those copies are generated and replaced from the
sources above. Obsidian is a memory store, not a skill source.

## Prompts

Prompt IDs select a unique Markdown file under `prompts/souls/`,
`prompts/executors/`, or `prompts/reviewers/`. Defaults and personalization
cannot declare the same ID. See [Prompts and pipelines](prompts.md) for prompt
selection, pipeline files, and workspace-specific soul overrides.

## Validate in CI

Validate a checkout locally:

```bash
uv run pynchy validate-personalization data/personalization
```

The command accepts any repository path, so the personalization repository can
validate itself in CI by checking out the latest Pynchy and running:

```bash
cd ../pynchy
uv sync --locked
uv run pynchy validate-personalization "$GITHUB_WORKSPACE"
```

The example repository includes a GitHub Actions workflow with this contract.
Validation covers layered Pynchy settings, automation documents and prompt
paths, skill metadata, LiteLLM structure, and configured model route names.
