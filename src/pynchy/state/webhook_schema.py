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

-- TODO: Prune unconsumed self-echo markers. Dropped provider callbacks retain
-- their exact marker after no receipt can consume it.
CREATE TABLE IF NOT EXISTS linear_comment_self_echoes (
    account_name TEXT NOT NULL,
    comment_id TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    action TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (account_name, comment_id, issue_id, revision, action)
);

CREATE TABLE IF NOT EXISTS linear_issue_state_self_echoes (
    account_name TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    state_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    action TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (account_name, issue_id, state_id, revision, action)
);
"""
