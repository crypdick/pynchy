"""SQLite database layer.

All functions are async using aiosqlite.
Module-level connection, initialized by init_database().

This package is split into domain-specific submodules:
  schema       — DDL, column migrations, data migrations
  connection   — connection lifecycle, write utilities
  chats        — chat metadata
  events       — EventBus event persistence
  messages     — message storage and retrieval
  tasks        — scheduled task CRUD and run logging
  host_jobs    — host-level cron jobs
  sessions     — session tracking and router state
  groups       — registered groups and workspace profiles
"""

# Re-export every public symbol so that `from pynchy.state import X` keeps working.

from pynchy.state.action_intents import (
    ActionIntentCreateRequest,
    action_intent_to_dict,
    approve_action_intent,
    claim_action_intent,
    confirm_action_intent,
    create_action_intent,
    deny_action_intent,
    expire_action_intent,
    fail_action_intent,
    get_action_intent_by_request,
    list_action_intents,
    mark_action_intent_awaiting_approval,
    mark_action_intent_executing,
    mark_action_intent_outcome_unknown,
    recover_incomplete_action_intents,
)
from pynchy.state.canaries import (
    get_latest_canary_runs,
    get_recent_canary_runs,
    get_unresolved_canary_regressions,
    record_canary_run,
)
from pynchy.state.channel_cursors import (
    advance_cursors_atomic,
    get_channel_cursor,
    prune_stale_cursors,
    set_channel_cursor,
)
from pynchy.state.chats import (
    get_all_chats,
    get_chat_cleared_at,
    get_chat_jids_by_name,
    get_last_group_sync,
    set_chat_cleared_at,
    set_last_group_sync,
    store_chat_metadata,
    update_chat_name,
)
from pynchy.state.connection import _get_db, close_test_database, init_database, init_test_database
from pynchy.state.conversation_admission import admit_conversation_delivery
from pynchy.state.conversation_controls import (
    close_conversation_control,
    get_conversation_control_binding,
    get_conversation_control_by_thread,
    list_idle_conversation_ids,
    set_conversation_control_binding,
)
from pynchy.state.conversation_events import (
    get_conversation_event_pointers_since,
    store_conversation_event_pointer,
)
from pynchy.state.conversation_lookup import get_conversation_for_subject_key
from pynchy.state.conversation_recovery import (
    prepare_conversation_delivery_recovery,
    prepare_conversation_runtime_ownership_recovery,
)
from pynchy.state.conversation_routing import (
    claim_next_conversation_delivery,
    complete_conversation_delivery,
    get_conversation,
    get_conversation_delivery,
    get_conversation_for_subject,
    list_pending_conversation_ids,
    list_route_conversation_ids,
    rebind_conversation_workspace,
    release_conversation_delivery_claim,
    resolve_conversation,
    set_conversation_session,
)
from pynchy.state.deployments import (
    advance_deployment_baseline,
    claim_deployment,
    clear_pending_deployment,
    complete_deployment,
    get_deployment_state,
    initialize_deployment_state,
)
from pynchy.state.events import store_event
from pynchy.state.external_cursors import (
    get_external_provider_cursor,
    set_external_provider_cursor,
)
from pynchy.state.external_deliveries import (
    admit_external_delivery_receipt,
    get_external_delivery_receipt,
)
from pynchy.state.groups import (
    delete_workspace_profile,
    get_all_workspace_profiles,
    get_workspace_profile,
    rebind_workspace_profile,
    set_workspace_profile,
)
from pynchy.state.host_jobs import (
    create_host_job,
    delete_host_job,
    get_all_host_jobs,
    get_host_job_by_id,
    get_host_job_by_name,
    record_host_job_completion,
    update_host_job,
)
from pynchy.state.in_flight_controls import (
    consume_in_flight_control_message,
    finalize_in_flight_pause,
    request_in_flight_turn_control,
    resume_paused_in_flight_turn,
)
from pynchy.state.in_flight_turns import (
    begin_in_flight_turn,
    claim_in_flight_turn,
    clear_in_flight_turn,
    clear_unclaimed_in_flight_turn_for_task,
    complete_in_flight_turn,
    get_in_flight_turn,
    get_in_flight_turn_for_chat,
    get_in_flight_turn_for_group,
    get_in_flight_turn_for_task,
    get_in_flight_turns,
    get_oldest_resumable_turn_for_group,
    mark_in_flight_output_sent,
    prepare_in_flight_turn_recovery,
    release_in_flight_turn_claim,
    update_in_flight_session,
)
from pynchy.state.messages import (
    get_chat_history,
    get_latest_inbound_timestamp,
    get_messages_since,
    get_messaging_stats,
    get_new_messages,
    mark_message_as_host,
    message_exists,
    prune_messages_by_sender,
    store_message,
    store_message_direct,
)
from pynchy.state.outbound import (
    gc_delivered,
    get_pending_outbound,
    mark_delivered,
    mark_delivery_error,
    mark_delivery_succeeded,
    record_outbound,
    record_outbound_deliveries,
)
from pynchy.state.security_context import (
    RecentSecurityContext,
    SecurityContextMessage,
    SecurityContextRole,
    SecurityExecutionAuthority,
    SecurityExecutionAuthorityKind,
    load_recent_security_context,
)
from pynchy.state.sessions import (
    SessionSecurityTaint,
    clear_session,
    get_all_sessions,
    get_router_state,
    get_session,
    get_session_security_taint,
    mark_session_security_taint,
    save_router_state_batch,
    set_router_state,
    set_session,
)
from pynchy.state.tasks import (
    cancel_task_and_checkpoint,
    create_task,
    create_task_if_absent,
    delete_task,
    get_active_task_for_group,
    get_all_tasks,
    get_task_by_id,
    get_task_run_logs,
    get_tasks_for_group,
    log_task_run,
    rebind_task_root,
    record_task_completion,
    resume_task,
    update_task,
)
from pynchy.state.webhook_effects import (
    begin_webhook_effect,
    confirm_webhook_effect,
    fail_webhook_effect,
    mark_webhook_effect_executing,
    mark_webhook_effect_outcome_unknown,
    reconcile_webhook_effect_absent,
    recover_incomplete_webhook_effects,
)
from pynchy.state.webhook_models import WebhookAdmission, WebhookConversationRequest, WebhookReceipt
from pynchy.state.webhooks import (
    admit_webhook_conversation,
    admit_webhook_receipt,
    get_webhook_receipt,
)
from pynchy.state.work_item_bindings import (
    bind_work_item_execution_to_task,
    bind_work_item_execution_to_turn,
)
from pynchy.state.work_item_cancellation import cancel_work_item_execution
from pynchy.state.work_item_models import (
    WorkItemClaimConflictError,
    WorkItemClaimRequest,
    WorkItemTransitionRequest,
)
from pynchy.state.work_items import (
    begin_work_item_transition,
    create_work_item_claim,
    get_active_work_item_execution,
    get_latest_unresolved_work_item_transition,
    get_work_item_execution,
    get_work_item_execution_for_issue,
    get_work_item_execution_for_task,
    get_work_item_transition_by_request,
    list_work_item_executions,
    mark_work_item_delivery_delivered_for_turn,
    resolve_work_item_transition,
)

__all__ = [  # noqa: RUF022 — intentionally grouped by source module, not alphabetical
    # connection
    "_get_db",
    "close_test_database",
    "init_database",
    "init_test_database",
    # canaries
    "get_latest_canary_runs",
    "get_recent_canary_runs",
    "get_unresolved_canary_regressions",
    "record_canary_run",
    # action_intents
    "ActionIntentCreateRequest",
    "action_intent_to_dict",
    "approve_action_intent",
    "claim_action_intent",
    "confirm_action_intent",
    "create_action_intent",
    "deny_action_intent",
    "expire_action_intent",
    "fail_action_intent",
    "get_action_intent_by_request",
    "list_action_intents",
    "mark_action_intent_awaiting_approval",
    "mark_action_intent_executing",
    "mark_action_intent_outcome_unknown",
    "recover_incomplete_action_intents",
    # conversation_events
    "get_conversation_event_pointers_since",
    "store_conversation_event_pointer",
    # conversation_routing
    "admit_conversation_delivery",
    "claim_next_conversation_delivery",
    "complete_conversation_delivery",
    "close_conversation_control",
    "get_conversation",
    "get_conversation_control_binding",
    "get_conversation_control_by_thread",
    "get_conversation_delivery",
    "get_conversation_for_subject",
    "get_conversation_for_subject_key",
    "list_idle_conversation_ids",
    "list_pending_conversation_ids",
    "list_route_conversation_ids",
    "prepare_conversation_delivery_recovery",
    "prepare_conversation_runtime_ownership_recovery",
    "rebind_conversation_workspace",
    "release_conversation_delivery_claim",
    "resolve_conversation",
    "set_conversation_control_binding",
    "set_conversation_session",
    # deployments
    "advance_deployment_baseline",
    "claim_deployment",
    "clear_pending_deployment",
    "complete_deployment",
    "get_deployment_state",
    "initialize_deployment_state",
    # channel_cursors
    "advance_cursors_atomic",
    "get_channel_cursor",
    "prune_stale_cursors",
    "set_channel_cursor",
    # outbound
    "gc_delivered",
    "get_pending_outbound",
    "mark_delivered",
    "mark_delivery_error",
    "mark_delivery_succeeded",
    "record_outbound",
    "record_outbound_deliveries",
    # events
    "store_event",
    "RecentSecurityContext",
    "SecurityContextMessage",
    "SecurityContextRole",
    "SecurityExecutionAuthority",
    "SecurityExecutionAuthorityKind",
    "load_recent_security_context",
    # external_deliveries
    "admit_external_delivery_receipt",
    "get_external_delivery_receipt",
    "get_external_provider_cursor",
    "set_external_provider_cursor",
    # chats
    "get_all_chats",
    "get_chat_cleared_at",
    "get_chat_jids_by_name",
    "get_last_group_sync",
    "set_chat_cleared_at",
    "set_last_group_sync",
    "store_chat_metadata",
    "update_chat_name",
    # messages
    "get_chat_history",
    "get_latest_inbound_timestamp",
    "get_messages_since",
    "get_messaging_stats",
    "get_new_messages",
    "message_exists",
    "mark_message_as_host",
    "prune_messages_by_sender",
    "store_message",
    "store_message_direct",
    # tasks
    "cancel_task_and_checkpoint",
    "create_task",
    "create_task_if_absent",
    "delete_task",
    "get_active_task_for_group",
    "get_all_tasks",
    "get_task_by_id",
    "get_task_run_logs",
    "get_tasks_for_group",
    "log_task_run",
    "record_task_completion",
    "rebind_task_root",
    "resume_task",
    "update_task",
    # webhooks
    "WebhookAdmission",
    "WebhookConversationRequest",
    "WebhookReceipt",
    "admit_webhook_conversation",
    "admit_webhook_receipt",
    "begin_webhook_effect",
    "confirm_webhook_effect",
    "fail_webhook_effect",
    "get_webhook_receipt",
    "mark_webhook_effect_executing",
    "mark_webhook_effect_outcome_unknown",
    "reconcile_webhook_effect_absent",
    "recover_incomplete_webhook_effects",
    # host_jobs
    "create_host_job",
    "delete_host_job",
    "get_all_host_jobs",
    "get_host_job_by_id",
    "get_host_job_by_name",
    "update_host_job",
    "record_host_job_completion",
    # in_flight_turns
    "begin_in_flight_turn",
    "claim_in_flight_turn",
    "clear_in_flight_turn",
    "clear_unclaimed_in_flight_turn_for_task",
    "complete_in_flight_turn",
    "consume_in_flight_control_message",
    "finalize_in_flight_pause",
    "get_in_flight_turn",
    "get_in_flight_turn_for_chat",
    "get_in_flight_turn_for_group",
    "get_in_flight_turn_for_task",
    "get_in_flight_turns",
    "get_oldest_resumable_turn_for_group",
    "mark_in_flight_output_sent",
    "prepare_in_flight_turn_recovery",
    "release_in_flight_turn_claim",
    "request_in_flight_turn_control",
    "resume_paused_in_flight_turn",
    "update_in_flight_session",
    # work_items
    "WorkItemClaimConflictError",
    "WorkItemClaimRequest",
    "WorkItemTransitionRequest",
    "begin_work_item_transition",
    "bind_work_item_execution_to_turn",
    "bind_work_item_execution_to_task",
    "cancel_work_item_execution",
    "create_work_item_claim",
    "get_active_work_item_execution",
    "get_latest_unresolved_work_item_transition",
    "get_work_item_execution",
    "get_work_item_execution_for_issue",
    "get_work_item_execution_for_task",
    "get_work_item_transition_by_request",
    "list_work_item_executions",
    "mark_work_item_delivery_delivered_for_turn",
    "resolve_work_item_transition",
    # sessions
    "SessionSecurityTaint",
    "clear_session",
    "get_all_sessions",
    "get_router_state",
    "get_session",
    "get_session_security_taint",
    "mark_session_security_taint",
    "save_router_state_batch",
    "set_router_state",
    "set_session",
    # groups
    "delete_workspace_profile",
    "get_all_workspace_profiles",
    "get_workspace_profile",
    "rebind_workspace_profile",
    "set_workspace_profile",
]
