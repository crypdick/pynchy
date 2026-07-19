"""SQLite schema owned by durable webhook admission."""

WEBHOOK_SCHEMA = """\
CREATE TABLE IF NOT EXISTS webhook_receipts (
    provider TEXT NOT NULL,
    route TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    workspace TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_action TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    disposition TEXT NOT NULL,
    ignored_reason TEXT,
    task_id TEXT UNIQUE,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (provider, route, delivery_id),
    FOREIGN KEY (task_id) REFERENCES scheduled_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_webhook_receipts_workspace
ON webhook_receipts(workspace, received_at DESC);
"""
