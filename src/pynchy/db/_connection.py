"""Database connection, schema, and migration internals.

Single module-level connection, initialized by init_database().
"""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from pynchy.config import get_settings
from pynchy.logger import logger

_db: aiosqlite.Connection | None = None

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
    project_access INTEGER DEFAULT 0
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

CREATE TABLE IF NOT EXISTS router_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    group_folder TEXT PRIMARY KEY,
    session_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS registered_groups (
    jid TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    folder TEXT NOT NULL UNIQUE,
    trigger_pattern TEXT NOT NULL,
    added_at TEXT NOT NULL,
    container_config TEXT,
    requires_trigger INTEGER DEFAULT 1,
    security_profile TEXT,
    is_god INTEGER DEFAULT 0
);
"""


def _get_db() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    return _db


async def _update_by_id(
    table: str,
    row_id: str,
    updates: dict[str, Any],
    allowed_fields: set[str],
) -> None:
    """Build and execute a dynamic UPDATE for an allowlisted set of fields.

    Shared by tasks, host_jobs, and any future table that needs
    partial-update-by-primary-key semantics.  Silently skips keys
    not in *allowed_fields* so callers don't need to pre-filter.
    """
    fields: list[str] = []
    values: list[Any] = []

    for key, value in updates.items():
        if key in allowed_fields:
            fields.append(f"{key} = ?")
            values.append(value)

    if not fields:
        return

    values.append(row_id)
    db = _get_db()
    await db.execute(
        f"UPDATE {table} SET {', '.join(fields)} WHERE id = ?",
        values,
    )
    await db.commit()


async def _create_schema(database: aiosqlite.Connection) -> None:
    await database.executescript(_SCHEMA)


async def init_database() -> None:
    """Initialize the database connection and schema."""
    global _db
    db_path = get_settings().store_dir / "messages.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    _db = await aiosqlite.connect(str(db_path))
    _db.row_factory = aiosqlite.Row
    await _create_schema(_db)
    await _migrate_json_state()


async def _init_test_database() -> None:
    """Create an in-memory database for tests."""
    global _db
    if _db is not None:
        await _db.close()
    _db = await aiosqlite.connect(":memory:")
    _db.row_factory = aiosqlite.Row
    await _create_schema(_db)


async def _migrate_json_state() -> None:
    """Migrate state from legacy JSON files to SQLite."""
    # Import here to avoid circular imports — these functions live in sibling modules
    # but they depend on _get_db() which lives here.
    from pynchy.db.groups import set_registered_group
    from pynchy.db.sessions import set_router_state, set_session
    from pynchy.types import RegisteredGroup

    def _read_and_archive(filename: str) -> Any | None:
        filepath = get_settings().data_dir / filename
        if not filepath.exists():
            return None
        try:
            data = json.loads(filepath.read_text())
            filepath.rename(filepath.with_suffix(filepath.suffix + ".migrated"))
            return data
        except Exception as exc:
            logger.warning("Failed to read/archive migration file", file=filename, err=str(exc))
            return None

    router_state = _read_and_archive("router_state.json")
    if router_state:
        if router_state.get("last_timestamp"):
            await set_router_state("last_timestamp", router_state["last_timestamp"])
        if router_state.get("last_agent_timestamp"):
            await set_router_state(
                "last_agent_timestamp",
                json.dumps(router_state["last_agent_timestamp"]),
            )

    sessions = _read_and_archive("sessions.json")
    if sessions:
        for folder, session_id in sessions.items():
            await set_session(folder, session_id)

    groups = _read_and_archive("registered_groups.json")
    if groups:
        for jid, group_data in groups.items():
            group = RegisteredGroup(
                name=group_data["name"],
                folder=group_data["folder"],
                trigger=group_data["trigger"],
                added_at=group_data["added_at"],
                container_config=group_data.get("containerConfig"),
                requires_trigger=group_data.get("requiresTrigger"),
            )
            await set_registered_group(jid, group)
