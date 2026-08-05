"""DDL for bounded scheduler-audit evidence."""

SCHEDULER_EVIDENCE_SCHEMA = """\
CREATE TABLE IF NOT EXISTS scheduler_definitions (
    definition_hash TEXT PRIMARY KEY,
    schedule_key TEXT NOT NULL,
    schedule_type TEXT NOT NULL,
    schedule_value TEXT NOT NULL,
    timezone TEXT NOT NULL,
    active_from TEXT NOT NULL,
    active_to TEXT,
    retention_watermark TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scheduler_definitions_key
ON scheduler_definitions(schedule_key, active_from);

CREATE TABLE IF NOT EXISTS scheduler_occurrences (
    definition_hash TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    dispatched_at TEXT,
    terminal_at TEXT,
    reason TEXT,
    workflow_id TEXT,
    run_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (definition_hash, scheduled_at),
    FOREIGN KEY (definition_hash) REFERENCES scheduler_definitions(definition_hash)
);
CREATE INDEX IF NOT EXISTS idx_scheduler_occurrences_range
ON scheduler_occurrences(definition_hash, scheduled_at);
"""
