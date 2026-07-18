"""DDL fragment for durable external-action records."""

from __future__ import annotations

ACTION_INTENT_SCHEMA = """\
CREATE TABLE IF NOT EXISTS action_intents (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    workspace TEXT NOT NULL,
    action_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    provider TEXT NOT NULL,
    actor_jid TEXT NOT NULL,
    recipient TEXT NOT NULL,
    payload TEXT NOT NULL,
    source_refs TEXT NOT NULL,
    summary TEXT NOT NULL,
    policy_decision TEXT NOT NULL,
    approver TEXT,
    approved_at TEXT,
    status TEXT NOT NULL,
    claimed_at TEXT,
    execution_started_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    provider_request_id TEXT,
    provider_receipt TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_action_intents_workspace
ON action_intents(workspace, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_action_intents_active
ON action_intents(status, updated_at DESC)
WHERE status IN ('awaiting_approval', 'approved', 'claimed', 'executing', 'outcome_unknown');
"""
