"""SQLite database layer.

All functions are async using aiosqlite.
Module-level connection, initialized by init_database().

This package is split into domain-specific submodules:
  _connection  — schema, init, migration
  chats        — chat metadata
  messages     — message storage and retrieval
  tasks        — scheduled task CRUD and run logging
  host_jobs    — host-level cron jobs
  sessions     — session tracking and router state
  groups       — registered groups and workspace profiles
"""

# Re-export every public symbol so that `from pynchy.db import X` keeps working.

from pynchy.db._connection import _get_db, _init_test_database, init_database
from pynchy.db.chats import (
    get_all_chats,
    get_last_group_sync,
    set_chat_cleared_at,
    set_last_group_sync,
    store_chat_metadata,
    update_chat_name,
)
from pynchy.db.groups import (
    get_all_registered_groups,
    get_all_workspace_profiles,
    get_registered_group,
    get_workspace_profile,
    set_registered_group,
    set_workspace_profile,
)
from pynchy.db.host_jobs import (
    create_host_job,
    delete_host_job,
    get_all_host_jobs,
    get_due_host_jobs,
    get_host_job_by_id,
    get_host_job_by_name,
    update_host_job,
    update_host_job_after_run,
)
from pynchy.db.messages import (
    get_chat_history,
    get_messages_since,
    get_new_messages,
    store_message,
    store_message_direct,
)
from pynchy.db.sessions import (
    clear_session,
    get_all_sessions,
    get_router_state,
    get_session,
    set_router_state,
    set_session,
)
from pynchy.db.tasks import (
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
    # chats
    "get_all_chats",
    "get_last_group_sync",
    "set_chat_cleared_at",
    "set_last_group_sync",
    "store_chat_metadata",
    "update_chat_name",
    # messages
    "get_chat_history",
    "get_messages_since",
    "get_new_messages",
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
    "set_router_state",
    "set_session",
    # groups
    "get_all_registered_groups",
    "get_all_workspace_profiles",
    "get_registered_group",
    "get_workspace_profile",
    "set_registered_group",
    "set_workspace_profile",
]
