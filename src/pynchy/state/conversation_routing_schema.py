"""SQLite schema owned by provider-neutral conversation routing."""

CONVERSATION_ROUTING_SCHEMA = """\
CREATE TABLE IF NOT EXISTS external_receipts (
    provider TEXT NOT NULL,
    route TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (provider, route, delivery_id)
);
INSERT OR IGNORE INTO external_receipts (
    provider, route, delivery_id, payload_sha256, received_at
)
SELECT provider, route, delivery_id, payload_sha256, received_at FROM webhook_receipts;

CREATE TABLE IF NOT EXISTS routed_conversations (
    id TEXT PRIMARY KEY,
    workspace TEXT NOT NULL,
    subject_namespace TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    session_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (subject_namespace, subject_key)
);
CREATE INDEX IF NOT EXISTS idx_routed_conversations_workspace
ON routed_conversations(workspace, updated_at DESC);

CREATE TABLE IF NOT EXISTS conversation_deliveries (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    route TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    received_at TEXT NOT NULL,
    claim_id TEXT,
    claimed_at TEXT,
    completed_at TEXT,
    UNIQUE (provider, route, delivery_id),
    FOREIGN KEY(provider,route,delivery_id)REFERENCES external_receipts(provider,route,delivery_id),
    FOREIGN KEY (conversation_id) REFERENCES routed_conversations(id)
);
CREATE INDEX IF NOT EXISTS idx_conversation_deliveries_fifo
ON conversation_deliveries(conversation_id, status, sequence);
CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_deliveries_claim
ON conversation_deliveries(claim_id) WHERE claim_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_deliveries_one_claim
ON conversation_deliveries(conversation_id) WHERE status = 'claimed';

CREATE TABLE IF NOT EXISTS conversation_control_bindings (
    conversation_id TEXT PRIMARY KEY,
    surface TEXT NOT NULL,
    parent_workspace TEXT NOT NULL,
    parent_jid TEXT NOT NULL,
    thread_jid TEXT NOT NULL,
    title TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES routed_conversations(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_control_thread
ON conversation_control_bindings(surface, thread_jid);
"""
