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

CREATE TABLE IF NOT EXISTS webhook_effects (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    account TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_action TEXT NOT NULL,
    subject_id TEXT,
    intent_fingerprint TEXT,
    status TEXT NOT NULL,
    fingerprint TEXT,
    created_at TEXT NOT NULL,
    executing_at TEXT,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_webhook_effects_scope
ON webhook_effects(
    provider, account, event_type, event_action, subject_id, status, fingerprint
);

CREATE TABLE IF NOT EXISTS webhook_effect_candidates (
    provider TEXT NOT NULL,
    route TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    effect_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    PRIMARY KEY (provider, route, delivery_id, effect_id),
    FOREIGN KEY(provider,route,delivery_id)REFERENCES webhook_receipts(provider,route,delivery_id),
    FOREIGN KEY (effect_id) REFERENCES webhook_effects(id)
);
CREATE INDEX IF NOT EXISTS idx_webhook_effect_candidates_effect
ON webhook_effect_candidates(effect_id);

CREATE TABLE IF NOT EXISTS webhook_effect_decisions (
    provider TEXT NOT NULL,
    route TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT,
    decided_at TEXT NOT NULL,
    PRIMARY KEY (provider, route, delivery_id),
    FOREIGN KEY(provider,route,delivery_id)REFERENCES webhook_receipts(provider,route,delivery_id)
);
"""
