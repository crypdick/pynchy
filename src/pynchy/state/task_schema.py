"""Current scheduled-task table definitions."""

TASK_SCHEMA = """\
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id TEXT PRIMARY KEY,
    group_folder TEXT NOT NULL,
    chat_jid TEXT NOT NULL,
    prompt TEXT NOT NULL,
    schedule_type TEXT NOT NULL,
    schedule_value TEXT NOT NULL,
    next_run TEXT,
    last_run TEXT,
    last_result TEXT,
    status TEXT DEFAULT 'active',
    created_at TEXT NOT NULL,
    memory_enabled INTEGER NOT NULL DEFAULT 1,
    session_policy TEXT NOT NULL DEFAULT 'reset_before_run',
    repo_access TEXT,
    input_source TEXT NOT NULL DEFAULT 'scheduled_task',
    config_job_name TEXT,
    config_job_is_deterministic INTEGER,
    config_job_command TEXT,
    config_job_cwd TEXT,
    config_job_timeout_seconds INTEGER,
    config_job_display_name TEXT,
    config_job_pre_run_command TEXT,
    config_job_pre_run_cwd TEXT,
    config_job_pre_run_timeout_seconds INTEGER,
    derived_thread_name TEXT,
    bound_chat_jid TEXT,
    bound_group_folder TEXT,
    conversation_id TEXT,
    last_reset_occurrence TEXT,
    occurrence_generation INTEGER NOT NULL DEFAULT 0,
    occurrence_due_at TEXT,
    superseded_occurrence_generation INTEGER,
    superseded_occurrence_due_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_next_run ON scheduled_tasks(next_run);
CREATE INDEX IF NOT EXISTS idx_status ON scheduled_tasks(status);
CREATE INDEX IF NOT EXISTS idx_group_folder ON scheduled_tasks(group_folder);

CREATE TABLE IF NOT EXISTS task_run_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    run_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    error TEXT,
    temporal_workflow_id TEXT,
    temporal_workflow_run_id TEXT,
    temporal_attempt INTEGER,
    turn_id TEXT,
    error_signature TEXT,
    escalation_reason TEXT,
    FOREIGN KEY (task_id) REFERENCES scheduled_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_task_run_logs ON task_run_logs(task_id, run_at);

"""
