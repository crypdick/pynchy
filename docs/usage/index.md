# Usage

Day-to-day operation of Pynchy — managing groups, scheduling tasks, and talking to your agents.

## What You Can Do

- **[Channels](../channels/index.md)** — Message your assistant from WhatsApp, Slack, or Discord (plugin-provided — more can be added)
- **[Workspaces](workspaces.md)** — Bind a chat to reusable profiles for prompts, tools, skills, repositories, and security policy
- **[Tool access and secrets](tool-access.md)** — Grant provider access through tools and limit credentials to the process that needs them
- **Admin channel** — Your private channel (self-chat) for admin control; every other group is completely isolated
- **[Persistent memory](memory.md)** — Agents search and update the mounted Obsidian vault across sessions and workspaces
- **[Scheduled tasks](scheduled-tasks.md)** — Recurring jobs that run the selected agent core and can message you back
- **[Personalization repository](personalization.md)** — Keep settings, LiteLLM routes, skills, and automations in an independent private repository
- **[Channel-scoped secrets](secrets.md)** — Grant Discord channels exact Vaultwarden collections without exposing vault credentials to agents
- **[Agent cores](agent-cores.md)** — Choose which LLM powers your agents — Claude SDK, OpenAI SDK, Codex CLI, or plugin-provided cores
- **[Control plane](control-plane.md)** — Inspect local or remote operational state through fail-closed listeners and bearer authentication
- **[Integrations](../integrations/index.md)** — Connect your workspaces to Google, GitHub, mail, Slack, Linear, and other external services
- **Web access** — Search and fetch content through configured browser tools
- **Container isolation** — Agents sandboxed in Apple Container (macOS) or Docker (macOS/Linux)

## Talking to Your Assistant

Talk to your assistant with the trigger word (default: `@Pynchy`):

```
@Pynchy send an overview of the sales pipeline every weekday morning at 9am (has access to my Obsidian vault folder)
@Pynchy review the git history for the past week each Friday and update the README if there's drift
@Pynchy every Monday at 8am, compile news on AI developments from Hacker News and TechCrunch and message me a briefing
```

From the Admin channel (your self-chat), you can manage groups and tasks:
```
@Pynchy list all scheduled tasks across groups
@Pynchy pause the Monday briefing task
@Pynchy join the Family Chat group
```

## Messaging During Active Tasks

When the agent is busy (handling a user message or scheduled task), new messages behave differently depending on prefix:

**btw ...** adds context to in-flight work ("btw the file is in `/tmp/data.csv`"). The agent sees it as a follow-up message.

**todo ...** queues an item for later without derailing the current task ("todo also rename the config keys when you're done"). The agent views and manages the todo list via `list_todos` and `complete_todo` MCP tools.

A normal message (no prefix) interrupts the active task — the container stops and your new message starts fresh.

### Pause and Resume Unfinished Work

Send exactly `stop` or `pause` to freeze the current agent turn immediately.
Matching is case-insensitive and accepts the workspace trigger, such as
`@pynchy pause`. Longer sentences such as `please pause` are ordinary agent
messages, not control commands.

Pynchy acknowledges the command with ⏸️, hibernates the runtime, and retains
the unfinished checkpoint and provider conversation. Your next ordinary
message becomes guidance for that same turn; Pynchy reopens the same provider
thread and continues the original work once. Pausing cannot undo side effects
that completed before Pynchy received the command.

If no turn is running or queued, `stop` and `pause` only hibernate the existing runtime.
Autonomous work already queued behind a running turn is removed before that
turn stops. A queued one-shot task remains frozen until a message in its chat
or thread resumes it. A queued recurring occurrence stops without disabling or
editing its definition, so a later schedule trigger can run normally. For an
active scheduled turn, later triggers skip while its occurrence is frozen.

Use `reset context` to discard the current or frozen occurrence and its
provider conversation. A recurring task stays active, and its next occurrence
starts with fresh context. Existing finish commands such as `done` and
`end session` keep their finish-and-hibernate behavior; they do not preserve
unfinished work.

## Customizing

Start conversationally, then put repeatable channel, workspace, and security
policy in `data/personalization/pynchy.toml` when you need it to survive
restarts and deployments.
For example, you can ask Pynchy to help you:

- "Change the trigger word to @Bob"
- "Remember in the future to make responses shorter and more direct"
- "Add a custom greeting when I say good morning"
- "Store conversation summaries weekly"

## Detailed Guides

| Topic | What it covers |
|-------|---------------|
| [Channels](../channels/index.md) | WhatsApp, Slack, and Discord — multi-channel sync |
| [Control plane](control-plane.md) | Local Unix socket, remote bearer authentication, rate limits, and deployment access |
| [Groups](groups.md) | Group management, admin channel privileges |
| [Workspace configuration](workspaces.md) | Compose profiles and bind workspaces to configured chats |
| [Tool access and secrets](tool-access.md) | Tool-owned credentials, companion skills, process exposure, and missing access |
| [Memory](memory.md) | Obsidian recall, automatic learning, automation memory, and conversation archives |
| [Scheduled tasks](scheduled-tasks.md) | Task types, MCP tools, execution model |
| [Personalization repository](personalization.md) | Layered settings, file-backed automations, custom skills, and CI validation |
| [Agent cores](agent-cores.md) | LLM framework selection, LiteLLM gateway |
| [Prompts](prompts.md) | System prompt extensions via profiles and workspaces |
| [MCP servers](mcp.md) | Adding external tool servers, environment variables, multi-tenant setup |
| [Host capabilities](host-capabilities/index.md) | Computer use, screenshots, and local speech services for the host desktop |
| [Tool Trust](security.md) | Configure tool trust declarations — control when agents need human approval |
