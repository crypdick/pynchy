"""Current database schema definition."""

from __future__ import annotations

import aiosqlite

from pynchy.state.action_intent_schema import ACTION_INTENT_SCHEMA
from pynchy.state.external_routing_schema import EXTERNAL_ROUTING_SCHEMA
from pynchy.state.in_flight_turn_schema import IN_FLIGHT_TURN_SCHEMA
from pynchy.state.task_schema import TASK_SCHEMA

_FOREIGN_KEY_VIOLATION_ERROR = "SQLite foreign-key violation remains"

_SCHEMA = (
    """\
CREATE TABLE IF NOT EXISTS chats (
    jid TEXT PRIMARY KEY,
    name TEXT,
    last_message_time TEXT,
    cleared_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT,
    chat_jid TEXT,
    sender TEXT,
    sender_name TEXT,
    content TEXT,
    timestamp TEXT,
    is_from_me INTEGER,
    message_type TEXT DEFAULT 'user',
    metadata TEXT,
    PRIMARY KEY (id, chat_jid),
    FOREIGN KEY (chat_jid) REFERENCES chats(jid)
);
CREATE INDEX IF NOT EXISTS idx_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_by_chat ON messages(chat_jid, timestamp);
-- NOTE: Update docs/architecture/message-types.md "Database Schema" if this changes.
CREATE TABLE IF NOT EXISTS message_ingestion_order (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL,
    chat_jid TEXT NOT NULL,
    UNIQUE (message_id, chat_jid),
    FOREIGN KEY (message_id, chat_jid) REFERENCES messages(id, chat_jid) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_message_ingestion_by_chat
ON message_ingestion_order(chat_jid, sequence);
INSERT OR IGNORE INTO message_ingestion_order (message_id, chat_jid)
SELECT id, chat_jid FROM messages ORDER BY rowid;

"""
    + TASK_SCHEMA
    + """
CREATE TABLE IF NOT EXISTS host_jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    command TEXT NOT NULL,
    schedule_type TEXT NOT NULL,
    schedule_value TEXT NOT NULL,
    next_run TEXT,
    last_run TEXT,
    status TEXT DEFAULT 'active',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    cwd TEXT,
    timeout_seconds INTEGER DEFAULT 600,
    enabled INTEGER DEFAULT 1,
    memory_enabled INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_host_jobs_next_run ON host_jobs(next_run);
CREATE INDEX IF NOT EXISTS idx_host_jobs_status ON host_jobs(status);

CREATE TABLE IF NOT EXISTS channel_cursors (
    channel_name  TEXT NOT NULL,
    chat_jid      TEXT NOT NULL,
    direction     TEXT NOT NULL,
    cursor_value  TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (channel_name, chat_jid, direction)
);

CREATE TABLE IF NOT EXISTS outbound_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_jid      TEXT NOT NULL,
    content       TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    source        TEXT NOT NULL,
    FOREIGN KEY (chat_jid) REFERENCES chats(jid)
);
CREATE INDEX IF NOT EXISTS idx_outbound_ledger_jid ON outbound_ledger(chat_jid);

CREATE TABLE IF NOT EXISTS outbound_deliveries (
    ledger_id          INTEGER NOT NULL,
    channel_name       TEXT NOT NULL,
    operation          TEXT NOT NULL DEFAULT 'post',
    remote_message_id  TEXT,
    delivered_at       TEXT,
    error              TEXT,
    PRIMARY KEY (ledger_id, channel_name),
    FOREIGN KEY (ledger_id) REFERENCES outbound_ledger(id)
);
CREATE INDEX IF NOT EXISTS idx_outbound_deliveries_pending
    ON outbound_deliveries(channel_name) WHERE delivered_at IS NULL;

CREATE TABLE IF NOT EXISTS router_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deployment_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    applied_sha TEXT,
    applied_config_hash TEXT,
    pending_sha TEXT,
    pending_config_hash TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    group_folder TEXT PRIMARY KEY,
    session_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_security_taint (
    group_folder TEXT PRIMARY KEY,
    corruption_tainted INTEGER NOT NULL DEFAULT 0,
    secret_tainted INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_item_executions (
    id TEXT PRIMARY KEY,
    workspace TEXT NOT NULL,
    linear_issue_id TEXT NOT NULL,
    linear_issue_identifier TEXT NOT NULL,
    linear_issue_url TEXT NOT NULL,
    turn_id TEXT,
    task_id TEXT,
    attempt INTEGER NOT NULL DEFAULT 1,
    flow_id TEXT,
    temporal_workflow_id TEXT,
    initiated_by TEXT NOT NULL,
    observed_state_id TEXT NOT NULL,
    observed_state_name TEXT NOT NULL,
    observed_updated_at TEXT,
    status TEXT NOT NULL,
    summary TEXT,
    blocker TEXT,
    handoff_to TEXT,
    evidence_refs TEXT NOT NULL DEFAULT '[]',
    requester_delivery_status TEXT NOT NULL DEFAULT 'not_requested',
    requester_delivery_turn_id TEXT,
    requester_delivery_error TEXT,
    requester_delivered_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_work_item_executions_active_issue_v3
ON work_item_executions(linear_issue_id)
WHERE status IN ('claiming', 'in_progress', 'unknown');
CREATE INDEX IF NOT EXISTS idx_work_item_executions_workspace
ON work_item_executions(workspace, updated_at DESC);

CREATE TABLE IF NOT EXISTS work_item_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    operation TEXT NOT NULL,
    target_status TEXT NOT NULL,
    result_execution_status TEXT NOT NULL,
    evidence_refs TEXT NOT NULL DEFAULT '[]',
    summary TEXT,
    blocker TEXT,
    handoff_to TEXT,
    status TEXT NOT NULL,
    receipt TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (execution_id) REFERENCES work_item_executions(id)
);
CREATE INDEX IF NOT EXISTS idx_work_item_transitions_execution
ON work_item_transitions(execution_id, id DESC);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    chat_jid TEXT,
    timestamp TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_chat ON events(chat_jid);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp);

CREATE TABLE IF NOT EXISTS canary_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    scenario_id TEXT NOT NULL,
    action_ids TEXT NOT NULL,
    target_profile TEXT NOT NULL,
    code_revision TEXT NOT NULL,
    config_revision TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    error_class TEXT,
    evidence_refs TEXT NOT NULL,
    is_regression INTEGER NOT NULL DEFAULT 0,
    starts_regression INTEGER NOT NULL DEFAULT 0,
    is_recovery INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_canary_runs_scenario_target
ON canary_runs(scenario_id, target_profile, id DESC);
CREATE INDEX IF NOT EXISTS idx_canary_runs_regression
ON canary_runs(is_regression, id DESC);

CREATE TABLE IF NOT EXISTS conversation_events (
    event_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL,
    chat_jid TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    kind TEXT NOT NULL,
    sender TEXT NOT NULL,
    sender_name TEXT,
    message_type TEXT NOT NULL,
    source_message_id TEXT,
    content_preview TEXT NOT NULL,
    trace_ref TEXT NOT NULL,
    metadata TEXT DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_conversation_events_chat_time
ON conversation_events(chat_jid, timestamp);
CREATE INDEX IF NOT EXISTS idx_conversation_events_turn
ON conversation_events(turn_id);

CREATE TABLE IF NOT EXISTS registered_groups (
    jid TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    folder TEXT NOT NULL UNIQUE,
    trigger_pattern TEXT NOT NULL,
    added_at TEXT NOT NULL,
    container_config TEXT,
    security_profile TEXT,
    is_admin INTEGER DEFAULT 0
);
"""
    + IN_FLIGHT_TURN_SCHEMA
    + EXTERNAL_ROUTING_SCHEMA
    + ACTION_INTENT_SCHEMA
)


async def _assert_foreign_key_integrity(database: aiosqlite.Connection) -> None:
    cursor = await database.execute("PRAGMA foreign_key_check")
    violation = await cursor.fetchone()
    if violation is not None:
        raise RuntimeError(
            f"{_FOREIGN_KEY_VIOLATION_ERROR}: "
            f"table={violation[0]} rowid={violation[1]} parent={violation[2]}"
        )


async def create_schema(database: aiosqlite.Connection) -> None:
    """Apply the current schema to the target database."""
    await database.executescript(_SCHEMA)
    await _assert_foreign_key_integrity(database)
