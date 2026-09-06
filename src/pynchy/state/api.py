# allow: file-length - facade re-exports are state package's enforced public surface.
"""Public API for the built-in SQLite state package.
# allow: file-length - curated state facade preserves one explicit cross-package surface.

All functions are async using aiosqlite.
Module-level connection, initialized by init_database().

This package is split into domain-specific submodules:
  schema       — current database DDL
  connection   — connection lifecycle, write utilities
  chats        — chat metadata
  events       — EventBus event persistence
  messages     — message storage and retrieval
  tasks        — scheduled task CRUD and run logging
  host_jobs    — host-level cron jobs
  sessions     — session tracking and router state
  groups       — registered groups and workspace profiles
"""

# Keep the public surface explicit here. Cross-package imports target this module;
# implementation modules remain package-private by default.

from pynchy.security_context import (
    RecentSecurityContext,
    SecurityContextMessage,
    SecurityContextRole,
    SecurityExecutionAuthority,
    SecurityExecutionAuthorityKind,
)
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
    reconcile_action_intent,
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
    ConversationControlWorkspaceChangedError,
    apply_conversation_control_state,
    conversation_control_state_matches,
    get_conversation_control_binding,
    get_conversation_control_by_thread,
    list_idle_conversation_ids,
    retire_conversation_for_terminal,
    set_conversation_control_binding,
)
from pynchy.state.conversation_events import (
    get_conversation_event_pointers_since,
    store_conversation_event_pointer,
)
from pynchy.state.conversation_lookup import get_conversation_for_subject_key
from pynchy.state.conversation_recovery import (
    prepare_conversation_delivery_recovery,
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
from pynchy.state.conversation_terminal_runtime import get_terminal_conversation_retirement
from pynchy.state.deployments import (
    advance_deployment_baseline,
    claim_deployment,
    clear_pending_deployment,
    complete_deployment,
    get_deployment_state,
    initialize_deployment_state,
)
from pynchy.state.events import store_event
from pynchy.state.external_cursors import get_external_provider_cursor, set_external_provider_cursor
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
    set_workspace_profiles,
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
    message_cursor,
    message_exists,
    prune_messages_by_sender,
    store_message,
    store_message_direct,
    upgrade_message_cursor,
)
from pynchy.state.outbound import (
    OutboundDelivery,
    OutboundDeliveryOperation,
    PendingDelivery,
    gc_delivered,
    get_pending_outbound,
    mark_delivered,
    mark_delivery_error,
    mark_delivery_succeeded,
    record_outbound,
    record_outbound_deliveries,
)
from pynchy.state.runtime_session_recovery import (
    clear_runtime_session_references,
    clear_runtime_session_references_batch,
)
from pynchy.state.security_context import load_recent_security_context
from pynchy.state.sessions import (
    SessionSecurityTaint,
    clear_chat_pause,
    clear_session,
    get_all_sessions,
    get_router_state,
    get_session,
    get_session_security_taint,
    is_chat_paused,
    mark_session_security_taint,
    pause_chat,
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
    get_tasks_for_conversation,
    get_tasks_for_group,
    log_task_run,
    rebind_task_root,
    record_task_completion,
    resume_once_task_after_unclaimed_scheduled_turn,
    resume_task,
    resume_task_if_no_in_flight_turn,
    update_task,
)
from pynchy.state.webhook_effect_admission import classify_webhook_effect_callback
from pynchy.state.webhook_effects import (
    begin_webhook_effect,
    confirm_webhook_effect,
    fail_webhook_effect,
    list_webhook_effects,
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
from pynchy.state.work_item_cancellation import (
    cancel_work_item_execution,
    cancel_work_item_execution_if_lifecycle_current,
)
from pynchy.state.work_item_terminal_recovery import (
    retire_latest_terminal_work_item_conversation,
    retire_terminal_execution_resources_if_unowned,
)
from pynchy.state.work_item_transitions import (
    begin_work_item_transition,
    begin_work_item_transition_if_lifecycle_current,
    get_latest_reconcilable_work_item_transition,
    get_latest_unresolved_work_item_transition,
    get_work_item_transition_by_request,
    resolve_work_item_transition,
    resolve_work_item_transition_if_lifecycle_current,
)
from pynchy.state.work_items import (
    create_work_item_claim,
    get_active_work_item_execution,
    get_unfinished_work_item_execution,
    get_work_item_execution,
    get_work_item_execution_for_issue,
    get_work_item_execution_for_task,
    get_work_item_execution_for_turn,
    list_terminal_work_item_executions_needing_repair,
    list_work_item_executions,
    mark_work_item_delivery_delivered_for_turn,
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
    "reconcile_action_intent",
    "recover_incomplete_action_intents",
    # conversation_events
    "get_conversation_event_pointers_since",
    "store_conversation_event_pointer",
    # conversation_routing
    "admit_conversation_delivery",
    "apply_conversation_control_state",
    "ConversationControlWorkspaceChangedError",
    "claim_next_conversation_delivery",
    "complete_conversation_delivery",
    "conversation_control_state_matches",
    "get_conversation",
    "get_conversation_control_binding",
    "get_conversation_control_by_thread",
    "get_terminal_conversation_retirement",
    "get_conversation_delivery",
    "get_conversation_for_subject",
    "get_conversation_for_subject_key",
    "list_idle_conversation_ids",
    "list_pending_conversation_ids",
    "list_route_conversation_ids",
    "prepare_conversation_delivery_recovery",
    "rebind_conversation_workspace",
    "release_conversation_delivery_claim",
    "retire_conversation_for_terminal",
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
    "OutboundDelivery",
    "OutboundDeliveryOperation",
    "PendingDelivery",
    "get_pending_outbound",
    "mark_delivered",
    "mark_delivery_error",
    "mark_delivery_succeeded",
    "record_outbound",
    "record_outbound_deliveries",
    "clear_runtime_session_references",
    "clear_runtime_session_references_batch",
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
    "message_cursor",
    "message_exists",
    "mark_message_as_host",
    "prune_messages_by_sender",
    "store_message",
    "store_message_direct",
    "upgrade_message_cursor",
    # tasks
    "cancel_task_and_checkpoint",
    "create_task",
    "create_task_if_absent",
    "delete_task",
    "get_active_task_for_group",
    "get_all_tasks",
    "get_task_by_id",
    "get_task_run_logs",
    "get_tasks_for_conversation",
    "get_tasks_for_group",
    "log_task_run",
    "record_task_completion",
    "rebind_task_root",
    "resume_task",
    "resume_once_task_after_unclaimed_scheduled_turn",
    "resume_task_if_no_in_flight_turn",
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
    "list_webhook_effects",
    "classify_webhook_effect_callback",
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
    "begin_work_item_transition",
    "begin_work_item_transition_if_lifecycle_current",
    "bind_work_item_execution_to_turn",
    "bind_work_item_execution_to_task",
    "cancel_work_item_execution",
    "cancel_work_item_execution_if_lifecycle_current",
    "create_work_item_claim",
    "get_active_work_item_execution",
    "get_latest_reconcilable_work_item_transition",
    "get_latest_unresolved_work_item_transition",
    "get_work_item_execution",
    "get_work_item_execution_for_issue",
    "get_work_item_execution_for_task",
    "get_work_item_execution_for_turn",
    "get_unfinished_work_item_execution",
    "get_work_item_transition_by_request",
    "list_terminal_work_item_executions_needing_repair",
    "list_work_item_executions",
    "mark_work_item_delivery_delivered_for_turn",
    "retire_latest_terminal_work_item_conversation",
    "retire_terminal_execution_resources_if_unowned",
    "resolve_work_item_transition",
    "resolve_work_item_transition_if_lifecycle_current",
    # sessions
    "SessionSecurityTaint",
    "clear_chat_pause",
    "clear_session",
    "get_all_sessions",
    "get_router_state",
    "get_session",
    "get_session_security_taint",
    "is_chat_paused",
    "mark_session_security_taint",
    "pause_chat",
    "save_router_state_batch",
    "set_router_state",
    "set_session",
    # groups
    "delete_workspace_profile",
    "get_all_workspace_profiles",
    "get_workspace_profile",
    "rebind_workspace_profile",
    "set_workspace_profile",
    "set_workspace_profiles",
]
