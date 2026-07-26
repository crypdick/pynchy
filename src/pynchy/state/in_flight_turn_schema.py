"""SQLite schema owned by durable in-flight agent turns."""

IN_FLIGHT_TURN_SCHEMA = """\
CREATE TABLE IF NOT EXISTS in_flight_turns (
    turn_id TEXT PRIMARY KEY,
    chat_jid TEXT NOT NULL,
    group_folder TEXT NOT NULL,
    work_kind TEXT NOT NULL,
    input_messages TEXT NOT NULL,
    input_start_cursor TEXT NOT NULL,
    input_end_cursor TEXT NOT NULL,
    started_at TEXT NOT NULL,
    task_id TEXT,
    session_id TEXT,
    output_sent INTEGER NOT NULL DEFAULT 0,
    interrupted_at TEXT,
    deploy_id TEXT,
    claimed_at TEXT,
    conversation_claim_id TEXT,
    input_source TEXT NOT NULL DEFAULT 'user',
    control_state TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_in_flight_turns_chat
ON in_flight_turns(chat_jid, started_at);
CREATE INDEX IF NOT EXISTS idx_in_flight_turns_task
ON in_flight_turns(task_id, started_at);
"""
