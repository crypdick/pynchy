"""Database schema definition and migrations.

``_SCHEMA`` is the source of truth for the latest table definitions.
``CREATE TABLE IF NOT EXISTS`` handles brand-new databases.
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

_SCHEMA = """\
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
    context_mode TEXT DEFAULT 'isolated',
    repo_access TEXT
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
    FOREIGN KEY (task_id) REFERENCES scheduled_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_task_run_logs ON task_run_logs(task_id, run_at);

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
    enabled INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_host_jobs_next_run ON host_jobs(next_run);
CREATE INDEX IF NOT EXISTS idx_host_jobs_status ON host_jobs(status);

CREATE TABLE IF NOT EXISTS jid_aliases (
    alias_jid TEXT PRIMARY KEY,
    canonical_jid TEXT NOT NULL,
    channel_name TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jid_aliases_canonical ON jid_aliases(canonical_jid);

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
    ledger_id     INTEGER NOT NULL,
    channel_name  TEXT NOT NULL,
    delivered_at  TEXT,
    error         TEXT,
    PRIMARY KEY (ledger_id, channel_name),
    FOREIGN KEY (ledger_id) REFERENCES outbound_ledger(id)
);
CREATE INDEX IF NOT EXISTS idx_outbound_deliveries_pending
    ON outbound_deliveries(channel_name) WHERE delivered_at IS NULL;

CREATE TABLE IF NOT EXISTS router_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    group_folder TEXT PRIMARY KEY,
    session_id TEXT NOT NULL
);
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

CREATE TABLE IF NOT EXISTS registered_groups (
    jid TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    folder TEXT NOT NULL UNIQUE,
    trigger_pattern TEXT NOT NULL,
    added_at TEXT NOT NULL,
    container_config TEXT,
    security_profile TEXT,
    is_god INTEGER DEFAULT 0,
    is_admin INTEGER DEFAULT 0
);
"""


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
        for line in body.split("\n"):
            line = line.strip().rstrip(",")
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


async def _migrate_renamed_columns(database: aiosqlite.Connection) -> None:
    """Copy old column values to new renamed columns (idempotent).

    Only copies where new column is 0 and old is 1, so re-running is safe.
    """
    migrations = [
        ("registered_groups", "is_god", "is_admin"),
    ]
    for table, old_col, new_col in migrations:
        cursor = await database.execute(f"PRAGMA table_info({table})")
        cols = {row[1] for row in await cursor.fetchall()}
        if old_col in cols and new_col in cols:
            await database.execute(
                f"UPDATE {table} SET {new_col} = {old_col} WHERE {new_col} = 0 AND {old_col} = 1"
            )
    await database.commit()


async def _migrate_repo_access_column(database: aiosqlite.Connection) -> None:
    """Migrate pynchy_repo_access INTEGER -> repo_access TEXT (idempotent).

    1. If pynchy_repo_access column exists: copy truthy rows to repo_access.
    2. Drop pynchy_repo_access column.
    3. If project_access column still exists: drop it too.
    """
    cursor = await database.execute("PRAGMA table_info(scheduled_tasks)")
    cols = {row[1] for row in await cursor.fetchall()}

    if "pynchy_repo_access" in cols:
        # Migrate truthy rows -- use 'pynchy' as a migration placeholder slug.
        # Users must update their config.toml to set the real slug.
        if "repo_access" in cols:
            await database.execute(
                "UPDATE scheduled_tasks SET repo_access = 'pynchy' "
                "WHERE pynchy_repo_access = 1 AND repo_access IS NULL"
            )
        try:
            await database.execute("ALTER TABLE scheduled_tasks DROP COLUMN pynchy_repo_access")
            logger.info("Dropped scheduled_tasks.pynchy_repo_access column")
        except Exception as exc:
            logger.warning("Failed to drop pynchy_repo_access column", err=str(exc))

    if "project_access" in cols:
        try:
            await database.execute("ALTER TABLE scheduled_tasks DROP COLUMN project_access")
            logger.info("Dropped scheduled_tasks.project_access column")
        except Exception as exc:
            logger.warning("Failed to drop project_access column", err=str(exc))

    await database.commit()


async def _seed_channel_cursors(database: aiosqlite.Connection) -> None:
    """Seed channel_cursors from existing last_agent_timestamp (one-time migration).

    Reads the JSON-encoded per-group agent timestamps from router_state and
    creates inbound cursor rows so the new reconciler starts from where the
    old catch-up left off.  Only runs when channel_cursors is empty.
    """
    cursor = await database.execute("SELECT COUNT(*) FROM channel_cursors")
    (count,) = await cursor.fetchone()
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
    # Seed an inbound cursor for every channel that has an alias for each group.
    # We also seed from the canonical JID itself if it looks channel-native.
    alias_cursor = await database.execute(
        "SELECT alias_jid, canonical_jid, channel_name FROM jid_aliases"
    )
    alias_rows = await alias_cursor.fetchall()
    seen: set[tuple[str, str]] = set()
    for _alias_jid, canonical_jid, channel_name in alias_rows:
        ts = agent_timestamps.get(canonical_jid)
        if not ts:
            continue
        key = (channel_name, canonical_jid)
        if key in seen:
            continue
        seen.add(key)
        await database.execute(
            "INSERT OR IGNORE INTO channel_cursors"
            " (channel_name, chat_jid, direction, cursor_value, updated_at)"
            " VALUES (?, ?, 'inbound', ?, ?)",
            (channel_name, canonical_jid, ts, now),
        )

    # Also seed for canonical JIDs that are themselves channel-native
    # (e.g. slack:C123 workspaces with no alias).
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


async def create_schema(database: aiosqlite.Connection) -> None:
    """Apply schema DDL and run all migrations."""
    await database.executescript(_SCHEMA)
    await _ensure_columns(database)
    await _migrate_renamed_columns(database)
    await _migrate_repo_access_column(database)
    await _seed_channel_cursors(database)
