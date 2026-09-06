"""Curated public application use cases for driving adapters."""

from __future__ import annotations

import pluggy

from pynchy.host.orchestrator.action_intents import (
    execute_action_intent,
    policy_approval_timestamp,
    prepare_action_intent,
)
from pynchy.host.orchestrator.adapters import has_active_session, resolve_admin_notification_jid
from pynchy.host.orchestrator.concurrency import GroupQueue
from pynchy.host.orchestrator.config_refresh import (
    ConfigRefreshResult,
    ConfigRefreshRuntime,
    ConfigRefreshStatus,
    configure_config_refresh_runtime,
    refresh_host_config,
)
from pynchy.host.orchestrator.conversation_control import (
    ConversationControlRequest,
    ConversationWorkspaceContext,
    ensure_conversation_workspace,
)
from pynchy.host.orchestrator.deploy import (
    build_container_image,
    finalize_deploy,
    rollback_deploy_checkout,
)
from pynchy.host.orchestrator.messaging.formatter import (
    format_internal_tags,
    format_tool_preview,
)
from pynchy.host.orchestrator.messaging.formatters.base import RenderedMessage
from pynchy.host.orchestrator.messaging.formatters.text import TextFormatter
from pynchy.host.orchestrator.messaging.pending_questions import (
    PENDING_QUESTION_TIMEOUT_SECONDS,
    find_pending_for_jid,
    sweep_expired_questions,
)
from pynchy.host.orchestrator.messaging.reconciler import reconcile_all_channels
from pynchy.host.orchestrator.runtime_process_control import ContainerRuntimeOperations
from pynchy.host.orchestrator.scheduled_binding import (
    ScheduledTaskTerminalError,
    ensure_scheduled_task_binding,
    ensure_scheduled_task_conversation_open,
)
from pynchy.host.orchestrator.task_scheduler import run_scheduled_agent
from pynchy.host.orchestrator.terminal_task_retirement import (
    cancel_scheduled_host_job,
    cancel_scheduled_task,
)
from pynchy.host.orchestrator.workspace_artifacts import (
    cleanup_orphaned_workspace_artifacts,
    cleanup_workspace_artifacts,
)
from pynchy.host.orchestrator.workspace_config import (
    RuntimeWorkspacePolicy,
    load_resolved_config,
    load_resolved_tool_access,
    register_runtime_workspace_policy,
    static_workspace_folder,
    unregister_runtime_workspace_policy,
    update_profile_skill_policy,
)
from pynchy.host.orchestrator.workspace_placement import resolve_workspace_placement
from pynchy.plugins.api import AgentCoreSpec
from pynchy.turn_outcomes import (
    TurnOutcome,
)
from pynchy.workspace.api import (
    WorkspaceProfile,
)


async def dispatch_interrupted_turn(turn_id: str, deps: object) -> TurnOutcome:
    """Resume one durable turn after the application composition is ready."""
    from pynchy.host.orchestrator.interrupted_turns import (  # noqa: PLC0415 - interrupted turns import the messaging pipeline, which initializes learning capabilities.
        dispatch_interrupted_turn as dispatch,
    )

    return await dispatch(turn_id, deps)


def resolve_container_timeout(group: WorkspaceProfile, default_timeout: float) -> float:
    """Return the workspace override or configured default agent timeout."""
    if group.container_config and group.container_config.timeout:
        return group.container_config.timeout
    return default_timeout


def resolve_agent_core(
    plugin_manager: pluggy.PluginManager | None, default_core: str
) -> tuple[str, str]:
    """Resolve the selected plugin agent core to its runner module and class."""
    module = "agent_runner.cores.openai"
    class_name = "OpenAIAgentCore"
    if plugin_manager:
        cores = [
            core
            for core in plugin_manager.hook.pynchy_agent_core_info()
            if isinstance(core, AgentCoreSpec)
        ]
        core_info = next((core for core in cores if core.name == default_core), None)
        core_info = core_info or (cores[0] if cores else None)
        if core_info:
            module = core_info.module
            class_name = core_info.class_name
    return module, class_name


__all__ = [
    "PENDING_QUESTION_TIMEOUT_SECONDS",
    "ConfigRefreshResult",
    "ConfigRefreshRuntime",
    "ConfigRefreshStatus",
    "ContainerRuntimeOperations",
    "ConversationControlRequest",
    "ConversationWorkspaceContext",
    "GroupQueue",
    "RenderedMessage",
    "RuntimeWorkspacePolicy",
    "ScheduledTaskTerminalError",
    "TextFormatter",
    "build_container_image",
    "cancel_scheduled_host_job",
    "cancel_scheduled_task",
    "cleanup_orphaned_workspace_artifacts",
    "cleanup_workspace_artifacts",
    "configure_config_refresh_runtime",
    "dispatch_interrupted_turn",
    "ensure_conversation_workspace",
    "ensure_scheduled_task_binding",
    "ensure_scheduled_task_conversation_open",
    "execute_action_intent",
    "finalize_deploy",
    "find_pending_for_jid",
    "format_internal_tags",
    "format_tool_preview",
    "has_active_session",
    "load_resolved_config",
    "load_resolved_tool_access",
    "policy_approval_timestamp",
    "prepare_action_intent",
    "reconcile_all_channels",
    "refresh_host_config",
    "register_runtime_workspace_policy",
    "resolve_admin_notification_jid",
    "resolve_agent_core",
    "resolve_container_timeout",
    "resolve_workspace_placement",
    "rollback_deploy_checkout",
    "run_scheduled_agent",
    "static_workspace_folder",
    "sweep_expired_questions",
    "unregister_runtime_workspace_policy",
    "update_profile_skill_policy",
]
