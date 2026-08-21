"""Built-in action declarations for calendars, workspace state, and lifecycle."""

from __future__ import annotations

from pynchy.actions._contract import ActionSpec, ActionSurface, ActionTransport
from pynchy.actions._spec_helpers import agent_action, build_action

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
    agent_action("automation.list", "agent-tools", "List automations.", "list_automations"),
    agent_action(
        "automation.read",
        "agent-tools",
        "Read one automation definition.",
        "get_automation",
    ),
    agent_action(
        "automation.create",
        "agent-tools",
        "Create an automation definition.",
        "create_automation",
    ),
    agent_action(
        "automation.update",
        "agent-tools",
        "Update an automation definition.",
        "update_automation",
    ),
    agent_action("automation.pause", "agent-tools", "Pause an automation.", "pause_automation"),
    agent_action("automation.resume", "agent-tools", "Resume an automation.", "resume_automation"),
    agent_action("automation.delete", "agent-tools", "Delete an automation.", "delete_automation"),
    agent_action("todo.list", "agent-tools", "List workspace todos.", "list_todos"),
    agent_action("todo.complete", "agent-tools", "Mark a todo complete.", "complete_todo"),
    agent_action(
        "message.outbound.queue",
        "agent-tools",
        "Queue an outbound message.",
        "send_message",
    ),
    agent_action(
        "message.source.health",
        "agent-tools",
        "Read Pynchy messaging-source readiness and persisted ingress freshness.",
        "messaging_source_health",
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
        "skill.catalog.search",
        "agent-tools",
        "Search the available personalized Pynchy skills.",
        "search_skills",
    ),
    agent_action(
        "skill.access.request",
        "agent-tools",
        "Request one-time or persistent access to a Pynchy skill.",
        "request_skill_access",
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
        "Publish a workspace worktree as a pull request.",
        "sync_worktree_to_main",
    ),
    agent_action(
        "lifecycle.managed.feature.publish",
        "agent-tools",
        "Publish one manifest-bound managed feature as a pull request.",
        "publish_managed_feature",
    ),
    agent_action(
        "lifecycle.managed.feature.rebase",
        "agent-tools",
        "Rebase one manifest-bound managed feature onto its remote default branch.",
        "rebase_managed_feature",
    ),
    agent_action(
        "lifecycle.context.reset",
        "agent-tools",
        "Clear an agent session and start a new context.",
        "reset_context",
    ),
)
