"""Database schema definition and migrations.

``_SCHEMA`` is the source of truth for the latest table definitions.
``CREATE TABLE IF NOT EXISTS`` handles freshly created databases.
``_ensure_columns`` handles existing databases where tables predate newly
added columns -- it parses the schema string and issues ``ALTER TABLE ADD
COLUMN`` for anything missing.  No numbered migration files needed.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import aiosqlite

from pynchy.logger import logger
from pynchy.state.action_intent_schema import ACTION_INTENT_SCHEMA
from pynchy.state.chat_parents import backfill_missing_chat_parents
from pynchy.state.conversation_identity_migrations import (
    retire_duplicate_linear_conversations,
)
from pynchy.state.external_routing_schema import EXTERNAL_ROUTING_SCHEMA
from pynchy.state.in_flight_turn_schema import IN_FLIGHT_TURN_SCHEMA
from pynchy.state.in_flight_turn_schema_migrations import (
    drop_legacy_scheduled_runtime_metadata,
)
from pynchy.state.task_schema_migrations import (
    TASK_SCHEMA,
    clear_temporal_owned_next_runs,
    migrate_cached_task_thread_binding,
    migrate_scheduled_session_policy,
)
from pynchy.state.work_item_schema_migrations import (
    migrate_work_item_active_index,
    migrate_work_item_outcome_projection,
)

_CHANNEL_CURSORS_COUNT_MISSING_ERROR = "COUNT(*) query on channel_cursors returned no row"
_FOREIGN_KEY_VIOLATION_ERROR = "SQLite foreign-key violation remains after migration"

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


def _parse_schema_columns(schema: str) -> dict[str, list[tuple[str, str]]]:
    """Parse CREATE TABLE statements and return {table: [(col_name, col_def), ...]}."""
    tables: dict[str, list[tuple[str, str]]] = {}
    for match in re.finditer(
        r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\);",
        schema,
        re.DOTALL,
    ):
        table = match.group(1)
        body = match.group(2)
        cols: list[tuple[str, str]] = []
        for raw_line in body.split("\n"):
            line = raw_line.strip().rstrip(",")
            if not line or line.startswith("--"):
                continue
            # Skip constraints (PRIMARY KEY, FOREIGN KEY, UNIQUE, CHECK, INDEX)
            upper = line.upper()
            if any(upper.startswith(kw) for kw in ("PRIMARY", "FOREIGN", "UNIQUE", "CHECK")):
                continue
            # First word is the column name
            parts = line.split(None, 1)
            if len(parts) >= 2:
                cols.append((parts[0], line))
        tables[table] = cols
    return tables


async def _ensure_columns(database: aiosqlite.Connection) -> None:
    """Add any columns present in _SCHEMA but missing from existing tables."""
    expected = _parse_schema_columns(_SCHEMA)
    for table, columns in expected.items():
        cursor = await database.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        if not rows:  # table doesn't exist yet (CREATE TABLE IF NOT EXISTS handles it)
            continue
        existing = {row[1] for row in rows}  # row[1] = column name
        for col_name, col_def in columns:
            if col_name not in existing:
                await database.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
                logger.info("Added missing column", table=table, column=col_name)
    await database.commit()


async def _migrate_conversation_control_intent(database: aiosqlite.Connection) -> None:
    """Promote closed bindings into the conversation-level lifecycle intent."""
    await database.execute(
        """
        UPDATE routed_conversations AS conversation
        SET control_closed = 1
        WHERE control_closed = 0
          AND EXISTS (
              SELECT 1
              FROM conversation_control_bindings AS binding
              WHERE binding.conversation_id = conversation.id
                AND binding.closed = 1
          )
        """
    )
    await database.commit()


async def _rename_conversation_event_trace_ref(database: aiosqlite.Connection) -> None:
    """Rename the provider-specific projection ref column after the Phoenix rollback."""
    cursor = await database.execute("PRAGMA table_info(conversation_events)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "phoenix_ref" not in cols or "trace_ref" in cols:
        return

    await database.execute("ALTER TABLE conversation_events RENAME COLUMN phoenix_ref TO trace_ref")
    await database.commit()
    logger.info("Renamed conversation_events.phoenix_ref column to trace_ref")


async def _migrate_renamed_columns(database: aiosqlite.Connection) -> None:
    """Copy source column values into their renamed counterparts (idempotent).

    Only copies where the destination is 0 and the source is 1, so re-running is safe.
    """
    cursor = await database.execute("PRAGMA table_info(registered_groups)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "is_god" in cols and "is_admin" in cols:
        await database.execute(
            "UPDATE registered_groups SET is_admin = is_god WHERE is_admin = 0 AND is_god = 1"
        )
    await database.commit()


async def _drop_is_god_column(database: aiosqlite.Connection) -> None:
    """Drop the is_god column from registered_groups (idempotent).

    is_admin already holds these values (copied by _migrate_renamed_columns).
    """
    cursor = await database.execute("PRAGMA table_info(registered_groups)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "is_god" not in cols:
        return

    try:
        await database.execute("ALTER TABLE registered_groups DROP COLUMN is_god")
        await database.commit()
        logger.info("Dropped registered_groups.is_god column")
    except aiosqlite.OperationalError as exc:
        logger.warning("Failed to drop is_god column", err=str(exc))


async def _migrate_repo_access_column(database: aiosqlite.Connection) -> None:
    """Convert pynchy_repo_access INTEGER -> repo_access TEXT (idempotent).

    1. If pynchy_repo_access column exists: copy truthy rows to repo_access.
    2. Drop pynchy_repo_access column.
    3. If project_access column still exists: drop it too.
    """
    cursor = await database.execute("PRAGMA table_info(scheduled_tasks)")
    cols = {row[1] for row in await cursor.fetchall()}

    if "pynchy_repo_access" in cols:
        # Copy truthy rows using 'pynchy' as a placeholder slug.
        # Users must update personalized settings to set the real slug.
        # _ensure_columns runs before this migration, so repo_access exists.
        await database.execute(
            "UPDATE scheduled_tasks SET repo_access = 'pynchy' "
            "WHERE pynchy_repo_access = 1 AND repo_access IS NULL"
        )
        try:
            await database.execute("ALTER TABLE scheduled_tasks DROP COLUMN pynchy_repo_access")
            logger.info("Dropped scheduled_tasks.pynchy_repo_access column")
        except aiosqlite.OperationalError as exc:
            logger.warning("Failed to drop pynchy_repo_access column", err=str(exc))

    if "project_access" in cols:
        try:
            await database.execute("ALTER TABLE scheduled_tasks DROP COLUMN project_access")
            logger.info("Dropped scheduled_tasks.project_access column")
        except aiosqlite.OperationalError as exc:
            logger.warning("Failed to drop project_access column", err=str(exc))

    await database.commit()


async def _seed_channel_cursors(database: aiosqlite.Connection) -> None:
    """Seed channel_cursors from existing last_agent_timestamp (one-time migration).

    Reads the JSON-encoded per-group agent timestamps from router_state and
    creates inbound cursor rows so the reconciler resumes from the last
    processed agent timestamp.  Only runs when channel_cursors is empty.
    """
    cursor = await database.execute("SELECT COUNT(*) FROM channel_cursors")
    row = await cursor.fetchone()
    if row is None:
        raise RuntimeError(_CHANNEL_CURSORS_COUNT_MISSING_ERROR)
    count = row[0]
    if count > 0:
        return  # already seeded

    cursor = await database.execute(
        "SELECT value FROM router_state WHERE key = 'last_agent_timestamp'"
    )
    row = await cursor.fetchone()
    if not row:
        return

    try:
        agent_timestamps: dict[str, str] = json.loads(row[0])
    except (ValueError, TypeError):
        return

    now = datetime.now(UTC).isoformat()
    seen: set[tuple[str, str]] = set()

    # Seed for canonical JIDs that are themselves channel-native
    # (e.g. slack:C123 workspaces with channel-native JIDs).
    groups_cursor = await database.execute("SELECT jid FROM registered_groups")
    group_rows = await groups_cursor.fetchall()
    for (jid,) in group_rows:
        ts = agent_timestamps.get(jid)
        if not ts:
            continue
        # Detect channel from JID prefix
        if ":" in jid:
            channel_name = jid.split(":")[0]
            key = (channel_name, jid)
            if key not in seen:
                seen.add(key)
                await database.execute(
                    "INSERT OR IGNORE INTO channel_cursors"
                    " (channel_name, chat_jid, direction, cursor_value, updated_at)"
                    " VALUES (?, ?, 'inbound', ?, ?)",
                    (channel_name, jid, ts, now),
                )

    await database.commit()
    if seen:
        logger.info("Seeded channel_cursors from last_agent_timestamp", count=len(seen))


async def _repair_runtime_integrity(database: aiosqlite.Connection) -> None:
    await database.execute("BEGIN IMMEDIATE")
    try:
        chat_parents = await backfill_missing_chat_parents(database)
        retired_conversations = await retire_duplicate_linear_conversations(database)
        await database.commit()
    finally:
        if database.in_transaction:
            await database.rollback()
    if chat_parents or retired_conversations:
        logger.info(
            "Repaired runtime database integrity",
            chat_parents=chat_parents,
            retired_conversations=retired_conversations,
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
    """Apply schema DDL and run all migrations."""
    await database.executescript(_SCHEMA)
    await _rename_conversation_event_trace_ref(database)
    await _ensure_columns(database)
    await _repair_runtime_integrity(database)
    await _migrate_conversation_control_intent(database)
    await drop_legacy_scheduled_runtime_metadata(database)
    await _migrate_renamed_columns(database)
    await _drop_is_god_column(database)
    await _migrate_repo_access_column(database)
    await migrate_scheduled_session_policy(database)
    await migrate_cached_task_thread_binding(database)
    await migrate_work_item_active_index(database)
    await migrate_work_item_outcome_projection(database)
    await clear_temporal_owned_next_runs(database)
    await _seed_channel_cursors(database)
    await _assert_foreign_key_integrity(database)
