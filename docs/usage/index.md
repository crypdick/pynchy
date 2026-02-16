# Usage

This section covers day-to-day operation of Pynchy — managing groups, scheduling tasks, and interacting with your agents.

## What You Can Do

- **Channel I/O** — Message Claude from WhatsApp, Slack, or the built-in TUI
- **Isolated group context** — Each group has its own `CLAUDE.md` memory, isolated filesystem, and runs in its own container sandbox
- **God channel** — Your private channel (self-chat) for admin control; every other group is completely isolated
- **Scheduled tasks** — Recurring jobs that run Claude and can message you back
- **Web access** — Search and fetch content
- **Container isolation** — Agents sandboxed in Apple Container (macOS) or Docker (macOS/Linux)
- **Agent Swarms** — Spin up teams of specialized agents that collaborate on complex tasks

## Talking to Your Assistant

Talk to your assistant with the trigger word (default: `@Pynchy`):

```
@Pynchy send an overview of the sales pipeline every weekday morning at 9am (has access to my Obsidian vault folder)
@Pynchy review the git history for the past week each Friday and update the README if there's drift
@Pynchy every Monday at 8am, compile news on AI developments from Hacker News and TechCrunch and message me a briefing
```

From the God channel (your self-chat), you can manage groups and tasks:
```
@Pynchy list all scheduled tasks across groups
@Pynchy pause the Monday briefing task
@Pynchy join the Family Chat group
```

## Messaging During Active Tasks

When an agent already works on something (a user message or scheduled task), new messages behave differently depending on the prefix:

**btw ...** adds context to work already in progress ("btw the file is in `/tmp/data.csv`"). The agent sees it as a follow-up message.

**todo ...** queues items for the agent to handle later without derailing the current task ("todo also rename the config keys when you're done"). The agent views and manages the todo list via `list_todos` and `complete_todo` MCP tools.

Sending a normal message (no prefix) interrupts the active task — the container stops and your new message gets processed from scratch.

## Customizing

No configuration files to learn. Just tell Pynchy what you want:

- "Change the trigger word to @Bob"
- "Remember in the future to make responses shorter and more direct"
- "Add a custom greeting when I say good morning"
- "Store conversation summaries weekly"

## Detailed Guides

| Topic | What it covers |
|-------|---------------|
| [Groups](groups.md) | Group management, god channel privileges |
| [Scheduled tasks](scheduled-tasks.md) | Task types, MCP tools, execution model |
