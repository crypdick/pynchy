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
from pynchy.state.connection import _get_db, _init_test_database, init_database
from pynchy.state.events import store_event
from pynchy.state.groups import (
    delete_workspace_profile,
    get_all_workspace_profiles,
    get_workspace_profile,
    set_workspace_profile,
)
from pynchy.state.host_jobs import (
    create_host_job,
    delete_host_job,
    get_all_host_jobs,
    get_due_host_jobs,
    get_host_job_by_id,
    get_host_job_by_name,
    update_host_job,
    update_host_job_after_run,
)
from pynchy.state.messages import (
    get_chat_history,
    get_messages_since,
    get_messaging_stats,
    get_new_messages,
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
    record_outbound,
)
from pynchy.state.sessions import (
    clear_session,
    get_all_sessions,
    get_router_state,
    get_session,
    save_router_state_batch,
    set_router_state,
    set_session,
)
from pynchy.state.tasks import (
    create_task,
    delete_task,
    get_active_task_for_group,
    get_all_tasks,
    get_due_tasks,
    get_task_by_id,
    get_tasks_for_group,
    log_task_run,
    update_task,
    update_task_after_run,
)

__all__ = [
    # connection
    "_get_db",
    "_init_test_database",
    "init_database",
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
    "record_outbound",
    # events
    "store_event",
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
    "get_messages_since",
    "get_messaging_stats",
    "get_new_messages",
    "message_exists",
    "prune_messages_by_sender",
    "store_message",
    "store_message_direct",
    # tasks
    "create_task",
    "delete_task",
    "get_active_task_for_group",
    "get_all_tasks",
    "get_due_tasks",
    "get_task_by_id",
    "get_tasks_for_group",
    "log_task_run",
    "update_task",
    "update_task_after_run",
    # host_jobs
    "create_host_job",
    "delete_host_job",
    "get_all_host_jobs",
    "get_due_host_jobs",
    "get_host_job_by_id",
    "get_host_job_by_name",
    "update_host_job",
    "update_host_job_after_run",
    # sessions
    "clear_session",
    "get_all_sessions",
    "get_router_state",
    "get_session",
    "save_router_state_batch",
    "set_router_state",
    "set_session",
    # groups
    "delete_workspace_profile",
    "get_all_workspace_profiles",
    "get_workspace_profile",
    "set_workspace_profile",
]
