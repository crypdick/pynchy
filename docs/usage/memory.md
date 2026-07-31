# Memory

Pynchy uses your Obsidian vault as the durable memory store. Provider sessions
retain the current conversation, while vault notes carry reusable knowledge
across sessions and workspaces.

## Recall

Enable `[learning]`, set `[learning.obsidian].vault_root`, and select the
`obsidian-knowledge` skill in the workspace profile. Pynchy mounts the
configured vault directly at `/home/agent/memory`.

The agent decides when prior context is relevant and searches the vault through
the skill. Pynchy doesn't search every inbound message or inject matches into
the prompt.

## Automatic Learning

Automatic learning can use an Obsidian vault as a shared memory namespace.
Enable `[learning]` and set `[learning.obsidian].vault_root` to the vault root.
Pynchy mounts that root read-write at `/home/agent/memory` by default.

After each successful turn, Pynchy starts a Temporal learning review workflow.
The workflow runs a hidden reviewer agent that decides whether the turn
produced durable memory or skill updates. Memory goes to Obsidian; skills go to
`data/personalization/skills/`. Set `max_attempts` to control Temporal activity
retries for that reviewer.

The vault root is the global memory namespace. The hidden reviewer chooses existing semantic folders first, then falls back to `systems/pynchy/profiles/{profile}/memory` when no repo, machine, subject, or other existing folder clearly fits.

Personalized and agent-authored skills live in one shared registry at
`data/personalization/skills/<skill-name>/SKILL.md`. Every agent may create or
improve skills through `$PYNCHY_SKILLS_ROOT`. A profile chooses which skills its
workspaces may use through its existing `skills` list. Add a skill by its exact
name alongside the profile's other skills:

```toml
[profiles.research]
skills = ["core", "research-workflow"]
```

Pynchy applies that selection to cold containers, later warm-container turns,
and direct-host turns. A newly authored or updated skill appears on the next
turn without restarting Pynchy. Pynchy does not grant personalized skills
automatically. A tier selector such as `learned`, or `*`, permits matching
skills; prefer exact names when profiles need different access. See
[workspace configuration](../architecture/workspaces.md#profile-config-fields)
for the general selection rules.

### Conversation Archives

When a session compacts because its context is too long, the agent archives the
conversation as Markdown in the group's `conversations/` folder.

## Automation Memory

Scheduled jobs get a stable task-owned directory under
`wiki/systems/pynchy/automation-memory/<task-id>/`. Container jobs see it at
`/home/agent/automation-memory`; host jobs receive its canonical path through
`PYNCHY_AUTOMATION_MEMORY_DIR`.

Memory is enabled by default. Set `memory = false` in the job definition to
omit the directory without deleting existing notes.
