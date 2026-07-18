# Usage

Day-to-day operation of Pynchy — managing groups, scheduling tasks, talking to your agents.

## What You Can Do

- **[Channels](channels.md)** — Message your assistant from WhatsApp, Slack, Discord, or the built-in TUI (plugin-provided — more can be added)
- **[Workspaces](workspaces.md)** — Bind a chat to reusable profiles for prompts, tools, skills, repositories, and security policy
- **Admin channel** — Your private channel (self-chat) for admin control; every other group is completely isolated
- **[Persistent memory](memory.md)** — Agents save and recall facts across sessions using structured memory tools with ranked search (plugin-provided backend)
- **[Scheduled tasks](scheduled-tasks.md)** — Recurring jobs that run the selected agent core and can message you back
- **[Agent cores](agent-cores.md)** — Choose which LLM powers your agents — Claude SDK, OpenAI SDK, Codex CLI, or plugin-provided cores
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

## Customizing

Start conversationally, then put repeatable channel, workspace, and security
policy in `config.toml` when you need it to survive restarts and deployments.
For example, you can ask Pynchy to help you:

- "Change the trigger word to @Bob"
- "Remember in the future to make responses shorter and more direct"
- "Add a custom greeting when I say good morning"
- "Store conversation summaries weekly"

## Detailed Guides

| Topic | What it covers |
|-------|---------------|
| [Channels](channels.md) | WhatsApp, Slack, Discord, and TUI — multi-channel sync |
| [Groups](groups.md) | Group management, admin channel privileges |
| [Workspace configuration](workspaces.md) | Compose profiles and bind workspaces to configured chats |
| [Memory](memory.md) | Structured memory tools, file-based memory, conversation archives |
| [Scheduled tasks](scheduled-tasks.md) | Task types, MCP tools, execution model |
| [Agent cores](agent-cores.md) | LLM framework selection, LiteLLM gateway |
| [Prompts](prompts.md) | System prompt extensions via profiles and workspaces |
| [MCP servers](mcp.md) | Adding external tool servers, environment variables, multi-tenant setup |
| [Computer use](computer-use.md) | Drive a host desktop through replaceable provider plugins for real-browser and native-app workflows |
| [Matrix communications](matrix-gateway.md) | Read bridged chats and send approval-gated replies as the account owner |
| [Notebooks](notebooks.md) | Jupyter/Quarto notebook execution via MCP tools |
| [Proton Mail](proton-mail.md) | Read, send, and delete Proton Mail through a host-side MCP server |
| [Google Drive](gdrive.md) | Google Drive file access via OAuth2 MCP server |
| [Slack MCP](slack-mcp.md) | Slack read access via browser tokens (no admin required) |
| [Tool Trust](security.md) | Configure tool trust declarations — control when agents need human approval |
