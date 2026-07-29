# Prompts and pipelines

Use prompts to configure an agent's stable identity, execution contract, or
independent review contract without changing Python code.

## Organize prompts by context

Put each Markdown file in the directory that matches the context where Pynchy
loads it:

```text
data/personalization/prompts/
├── souls/
│   └── my-soul.md
├── executors/
│   └── research.md
└── reviewers/
    └── evidence.md
```

- A **soul** defines stable identity, values, and communication style.
- An **executor** defines what an agent should accomplish, its authority, and
  what success means. Pynchy composes the default executor with a stage-specific
  executor when the IDs differ.
- A **reviewer** evaluates executor work in a separate agent context.

The relative path without `.md` forms the prompt ID, such as
`souls/my-soul`. Prompt files must sit directly inside one of these three
directories and use lowercase hyphenated filenames.

Every prompt ID must remain unique across `data/defaults/prompts/` and
`data/personalization/prompts/`. Personalized prompts never shadow public
defaults. To replace a default, create a prompt with a new ID and select it
explicitly.

## Select global prompts

Set global prompt choices in `data/personalization/pynchy.toml`:

```toml
[prompts]
default_soul = "souls/my-soul"
default_executor = "executors/research"
default_pipeline = "research"

# Built-in independent reviewer contexts remain configurable.
cop_inbound = "reviewers/my-cop-inbound"
cop_outbound = "reviewers/my-cop-outbound"
cop_bash = "reviewers/my-cop-bash"
cop_taint = "reviewers/my-cop-taint"
learning = "reviewers/my-learning-review"
plan_freshness = "reviewers/my-plan-review"
```

Pynchy fails configuration validation when a selected prompt does not exist.
Security enforcement remains in host code even when an operator replaces the
Cop prompt text.

## Define a pipeline

Create one named pipeline per `pipelines/*.toml` file:

```toml
# data/personalization/pipelines/research.toml
schema_version = 1

[[pipeline.stages]]
name = "interactive"
executor = "executors/research"
reviewers = ["reviewers/evidence"]

[[pipeline.stages]]
name = "planning"
executor = "executors/research-plan"

[[pipeline.stages]]
name = "delivery"
executor = "executors/research"
reviewers = ["reviewers/evidence", "reviewers/citations"]

[[pipeline.stages]]
name = "follow-up"
executor = "executors/research-follow-up"
```

Pynchy recognizes `interactive`, `planning`, `delivery`, and `follow-up`
stages. A missing stage falls back to `interactive`. This lets a coding
workspace and a research workspace use the same Linear lifecycle with
different agents.

After a successful scheduled executor run, Pynchy runs each configured reviewer
in a separate agent context without the workspace's selected tools. Pynchy posts
the returned reviews to the workspace and records them with the task result. A
selected reviewer that fails to return a review fails the scheduled run.

## Select a workspace soul and pipeline

Give each workspace its own TOML file:

```toml
# data/personalization/workspaces/research.toml
schema_version = 1

[workspace]
profiles = ["research"]
soul = "souls/my-soul"
pipeline = "research"
chat = "connection.discord.mybot.chat.community.channels.research"
```

The filename forms the workspace name. Omit `soul` or `pipeline` to inherit the
global selection.

## Keep prompts stable

Prompt content becomes part of the provider session context. Changing a
selected prompt, pipeline, soul, or executor retires affected sessions so one
conversation cannot resume under mixed instructions.

Keep changing task data out of prompt files. Put scheduled job instructions
directly in the automation TOML and let runtime context carry issue data,
timestamps, and other per-run facts.

Repository instructions remain independent. A repo-backed workspace exposes
the instruction file understood by the selected core, such as `AGENTS.md` or
`CLAUDE.md`, alongside its selected Pynchy soul and executor.
