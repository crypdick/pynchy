"""Linear runtime wiring shared by the test configuration."""

from __future__ import annotations

from asyncio import sleep
from typing import TYPE_CHECKING

from pynchy.host.orchestrator.api import static_workspace_folder
from pynchy.plugins.integrations.linear_accounts import (
    LinearAccountRuntime,
    configure_linear_account_runtime,
    linear_account,
    linear_account_for_workspace,
)
from pynchy.plugins.integrations.linear_boot import (
    LinearBootRuntime,
    configure_linear_boot_runtime,
    configured_linear_workspace_names,
)
from pynchy.plugins.integrations.linear_conversation_identity import (
    LinearConversationRuntime,
    configure_linear_conversation_runtime,
)
from pynchy.plugins.integrations.linear_planning_tasks import (
    LinearPlanningTaskRuntime,
    configure_linear_planning_task_runtime,
)
from pynchy.plugins.integrations.linear_provider_reconciliation import (
    LinearDecisionInboxRuntime,
    configure_linear_decision_inbox_runtime,
)
from pynchy.plugins.integrations.linear_self_echoes import (
    LinearSelfEchoRuntime,
    configure_linear_self_echo_runtime,
)
from pynchy.plugins.integrations.linear_webhook_config import LinearPluginOptions
from pynchy.plugins.integrations.linear_webhook_effects import (
    LinearWebhookEffectsRuntime,
    configure_linear_webhook_effects_runtime,
)
from pynchy.plugins.integrations.linear_webhook_prompts import LinearWebhookPrompts
from pynchy.plugins.integrations.linear_webhooks import (
    LinearWebhookRuntime,
    configure_linear_webhook_runtime,
)
from pynchy.plugins.integrations.linear_work_item_completion import (
    LinearWorkItemCompletionRuntime,
    configure_linear_work_item_completion_runtime,
)
from pynchy.plugins.integrations.linear_work_item_provider import (
    LinearWorkItemRuntime,
    configure_linear_work_item_runtime,
)
from pynchy.plugins.integrations.linear_work_item_tasks import (
    LinearWorkItemTaskRuntime,
    configure_linear_work_item_task_runtime,
)
from pynchy.plugins.integrations.linear_work_items import (
    LinearWorkItemsRuntime,
    configure_linear_work_items_runtime,
)
from pynchy.state import (
    apply_conversation_control_state,
    begin_webhook_effect,
    begin_work_item_transition,
    begin_work_item_transition_if_lifecycle_current,
    bind_work_item_execution_to_task,
    bind_work_item_execution_to_turn,
    cancel_work_item_execution,
    cancel_work_item_execution_if_lifecycle_current,
    confirm_webhook_effect,
    conversation_control_state_matches,
    create_task_if_absent,
    create_work_item_claim,
    fail_webhook_effect,
    get_active_work_item_execution,
    get_all_tasks,
    get_conversation_control_binding,
    get_conversation_for_subject_key,
    get_in_flight_turn_for_group,
    get_latest_reconcilable_work_item_transition,
    get_latest_unresolved_work_item_transition,
    get_task_by_id,
    get_task_run_logs,
    get_unfinished_work_item_execution,
    get_work_item_execution,
    get_work_item_execution_for_issue,
    get_work_item_transition_by_request,
    list_terminal_work_item_executions_needing_repair,
    list_work_item_executions,
    mark_webhook_effect_executing,
    mark_webhook_effect_outcome_unknown,
    resolve_conversation,
    resolve_work_item_transition,
    resolve_work_item_transition_if_lifecycle_current,
    resume_once_task_after_unclaimed_scheduled_turn,
    update_task,
)

if TYPE_CHECKING:
    from pynchy.config.api import Settings


async def _noop_linear_reconciliation() -> None:
    pass


async def _noop_execution_retirement(_execution) -> None:
    pass


async def _noop_superseded_execution_retirement(_execution) -> bool:
    await sleep(0)
    return False


async def _noop_terminal_execution_retirement(_execution, _revision) -> None:
    pass


def configure_linear_accounts_for(
    settings: Settings,
    *,
    start_work_item_reconciliation=_noop_linear_reconciliation,
    retire_execution=_noop_execution_retirement,
    retire_terminal_execution=_noop_terminal_execution_retirement,
) -> None:
    """Wire Linear account lookups to one test's resolved settings."""

    def workspace_tool_names(workspace: str) -> tuple[str, ...] | None:
        resolved = settings.resolved_workspace_config(workspace)
        return tuple(resolved.tools) if resolved is not None else None

    configure_linear_boot_runtime(
        LinearBootRuntime(
            workspace_names=tuple(settings.workspace_names()),
            account_for_name=lambda name: linear_account(name, settings.tools),
            account_for_workspace=lambda workspace: linear_account_for_workspace(
                workspace,
                tools=settings.tools,
                workspace_tool_names=workspace_tool_names,
            ),
            workspace_parent=settings.workspace_parent,
            canonical_workspace_folder=static_workspace_folder,
            additional_workspaces=lambda _registered: (),
        )
    )

    plugin = settings.plugins.get("linear")
    configure_linear_webhook_runtime(
        LinearWebhookRuntime(
            options=LinearPluginOptions.model_validate(
                plugin.options if plugin is not None else {}
            ),
            prompts=LinearWebhookPrompts(
                issue="test issue instructions",
                comment="test comment instructions",
            ),
            account_for_name=lambda name: linear_account(name, settings.tools),
            workspace_tools=workspace_tool_names,
            workspace_names_for_account=configured_linear_workspace_names,
        )
    )

    configure_linear_account_runtime(
        LinearAccountRuntime(
            tools=settings.tools,
            workspace_tool_names=workspace_tool_names,
        )
    )
    configure_linear_conversation_runtime(
        LinearConversationRuntime(
            get_unfinished_execution=get_unfinished_work_item_execution,
            get_for_subject_key=lambda key, workspace, suffix: get_conversation_for_subject_key(
                key,
                workspace=workspace,
                namespace_suffix=suffix,
            ),
            get_control_binding=get_conversation_control_binding,
            resolve=resolve_conversation,
        )
    )
    configure_linear_webhook_effects_runtime(
        LinearWebhookEffectsRuntime(
            resolve_conversation=resolve_conversation,
            control_state_matches=conversation_control_state_matches,
            apply_control_state=apply_conversation_control_state,
            get_execution_for_issue=get_work_item_execution_for_issue,
            cancel_execution=cancel_work_item_execution,
            cancel_execution_if_lifecycle_current=cancel_work_item_execution_if_lifecycle_current,
            get_active_execution=get_active_work_item_execution,
            start_work_item_reconciliation=start_work_item_reconciliation,
        )
    )
    configure_linear_work_item_task_runtime(
        LinearWorkItemTaskRuntime(
            get_control_binding=get_conversation_control_binding,
            get_task=get_task_by_id,
            create_task=create_task_if_absent,
            update_task=update_task,
            get_task_logs=get_task_run_logs,
            bind_execution_to_task=bind_work_item_execution_to_task,
            get_active_execution=get_active_work_item_execution,
            resume_once_task=resume_once_task_after_unclaimed_scheduled_turn,
            get_execution_for_issue=get_work_item_execution_for_issue,
        )
    )
    configure_linear_decision_inbox_runtime(
        LinearDecisionInboxRuntime(
            list_executions=list_work_item_executions,
            list_terminal_repair_candidates=(list_terminal_work_item_executions_needing_repair),
            get_latest_unresolved_transition=get_latest_unresolved_work_item_transition,
            cancel_execution=cancel_work_item_execution,
            retire_execution=retire_execution,
            retire_terminal_execution_if_unowned=_noop_superseded_execution_retirement,
            retire_terminal_execution=retire_terminal_execution,
        )
    )
    configure_linear_work_items_runtime(
        LinearWorkItemsRuntime(
            list_executions=list_work_item_executions,
            get_active_execution=get_active_work_item_execution,
            get_execution_for_issue=get_work_item_execution_for_issue,
            get_in_flight_turn=get_in_flight_turn_for_group,
            bind_execution_to_turn=bind_work_item_execution_to_turn,
            get_latest_reconcilable_transition=get_latest_reconcilable_work_item_transition,
        )
    )
    configure_linear_work_item_runtime(
        LinearWorkItemRuntime(
            get_transition_by_request=get_work_item_transition_by_request,
            get_execution=get_work_item_execution,
            get_active_execution=get_active_work_item_execution,
            create_claim=create_work_item_claim,
            begin_transition=begin_work_item_transition,
            resolve_transition=resolve_work_item_transition,
            resolve_transition_if_lifecycle_current=resolve_work_item_transition_if_lifecycle_current,
        )
    )
    configure_linear_work_item_completion_runtime(
        LinearWorkItemCompletionRuntime(
            get_execution_for_issue=get_work_item_execution_for_issue,
            get_transition_by_request=get_work_item_transition_by_request,
            get_latest_unresolved_transition=get_latest_unresolved_work_item_transition,
            begin_transition=begin_work_item_transition,
            begin_transition_if_lifecycle_current=begin_work_item_transition_if_lifecycle_current,
        )
    )
    configure_linear_planning_task_runtime(LinearPlanningTaskRuntime(get_all_tasks=get_all_tasks))
    configure_linear_self_echo_runtime(
        LinearSelfEchoRuntime(
            begin=begin_webhook_effect,
            mark_executing=mark_webhook_effect_executing,
            confirm=confirm_webhook_effect,
            fail=fail_webhook_effect,
            mark_outcome_unknown=mark_webhook_effect_outcome_unknown,
        )
    )
