"""Built-in action declarations for calendars, workspace state, and lifecycle."""

from __future__ import annotations

from pynchy._action_contract import ActionSpec, ActionSurface, ActionTransport
from pynchy._action_spec_helpers import agent_action, build_action

CORE_ACTION_SPECS: tuple[ActionSpec, ...] = (
    agent_action(
        "calendar.calendar.list",
        "caldav",
        "Discover calendars available to the workspace.",
        "list_calendars",
        canary="calendar.round.trip",
    ),
    agent_action(
        "calendar.event.list",
        "caldav",
        "List events in a calendar and date range.",
        "list_calendar",
        canary="calendar.round.trip",
    ),
    agent_action(
        "calendar.event.create",
        "caldav",
        "Create an event in a selected calendar.",
        "create_event",
        canary="calendar.round.trip",
    ),
    agent_action(
        "calendar.event.delete",
        "caldav",
        "Delete an event by identifier.",
        "delete_event",
        canary="calendar.round.trip",
    ),
    agent_action("memory.save", "sqlite-memory", "Create or update a memory.", "save_memory"),
    agent_action(
        "memory.recall", "sqlite-memory", "Retrieve relevant memories.", "recall_memories"
    ),
    agent_action("memory.forget", "sqlite-memory", "Delete a memory.", "forget_memory"),
    agent_action("memory.list", "sqlite-memory", "List memories in a workspace.", "list_memories"),
    agent_action("task.schedule", "agent-tools", "Create a scheduled agent task.", "schedule_task"),
    agent_action("task.list", "agent-tools", "List scheduled tasks.", "list_tasks"),
    agent_action("task.pause", "agent-tools", "Pause a scheduled task.", "pause_task"),
    agent_action("task.resume", "agent-tools", "Resume a scheduled task.", "resume_task"),
    agent_action("task.cancel", "agent-tools", "Cancel a scheduled task.", "cancel_task"),
    agent_action("todo.list", "agent-tools", "List workspace todos.", "list_todos"),
    agent_action("todo.complete", "agent-tools", "Mark a todo complete.", "complete_todo"),
    agent_action(
        "message.outbound.queue",
        "agent-tools",
        "Queue an outbound message.",
        "send_message",
    ),
    build_action(
        "message.outbound.retry",
        "messaging",
        "Retry an undelivered outbound message.",
        ActionSurface(ActionTransport.HOST_WORKFLOW, "outbound_reconciler"),
        canary="channel.outbound.round.trip",
    ),
    agent_action(
        "user.question.ask",
        "messaging",
        "Ask a user and route their answer back to the agent.",
        "ask_user",
        canary="channel.ask.answer",
    ),
    agent_action(
        "workspace.group.register",
        "agent-tools",
        "Register a chat group as a Pynchy workspace.",
        "register_group",
    ),
    agent_action(
        "deployment.apply",
        "agent-tools",
        "Deploy committed Pynchy changes and resume the interrupted conversation.",
        "deploy_changes",
    ),
    agent_action(
        "lifecycle.worktree.sync",
        "agent-tools",
        "Publish a workspace worktree through the configured Git policy.",
        "sync_worktree_to_main",
    ),
    agent_action(
        "lifecycle.task.finish",
        "agent-tools",
        "Finish scheduled work and notify the workspace.",
        "finished_work",
    ),
    agent_action(
        "lifecycle.context.reset",
        "agent-tools",
        "Clear an agent session and start a new context.",
        "reset_context",
    ),
)
