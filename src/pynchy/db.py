"""SQLite database layer.

Port of src/db.ts — all functions are async using aiosqlite.
Module-level connection, initialized by init_database().
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from pynchy.config import DATA_DIR, STORE_DIR
from pynchy.types import ContainerConfig, NewMessage, RegisteredGroup, ScheduledTask, TaskRunLog

_db: aiosqlite.Connection | None = None

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS chats (
    jid TEXT PRIMARY KEY,
    name TEXT,
    last_message_time TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT,
    chat_jid TEXT,
    sender TEXT,
    sender_name TEXT,
    content TEXT,
    timestamp TEXT,
    is_from_me INTEGER,
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
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_next_run ON scheduled_tasks(next_run);
CREATE INDEX IF NOT EXISTS idx_status ON scheduled_tasks(status);

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
    requires_trigger INTEGER DEFAULT 1
);
"""


def _get_db() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    return _db


async def _create_schema(database: aiosqlite.Connection) -> None:
    await database.executescript(_SCHEMA)
    # Migration: add context_mode column if missing
    try:
        await database.execute(
            "ALTER TABLE scheduled_tasks ADD COLUMN context_mode TEXT DEFAULT 'isolated'"
        )
        await database.commit()
    except Exception:
        pass
    # Migration: add project_access column if missing
    try:
        await database.execute(
            "ALTER TABLE scheduled_tasks ADD COLUMN project_access INTEGER DEFAULT 0"
        )
        await database.commit()
    except Exception:
        pass
    # Migration: add cleared_at column to chats
    try:
        await database.execute("ALTER TABLE chats ADD COLUMN cleared_at TEXT")
        await database.commit()
    except Exception:
        pass
    # Migration: add message_type column (Phase 1 of message types refactor)
    try:
        await database.execute("ALTER TABLE messages ADD COLUMN message_type TEXT DEFAULT 'user'")
        await database.commit()
    except Exception:
        pass
    # Migration: add metadata JSON column (Phase 1 of message types refactor)
    try:
        await database.execute("ALTER TABLE messages ADD COLUMN metadata TEXT")
        await database.commit()
    except Exception:
        pass
    # Migration: backfill message_type based on sender patterns
    try:
        # Host messages
        await database.execute(
            "UPDATE messages SET message_type = 'host' WHERE sender = 'host' AND message_type = 'user'"
        )
        # Tool result messages (command outputs)
        await database.execute(
            "UPDATE messages SET message_type = 'tool_result' "
            "WHERE sender = 'command_output' AND message_type = 'user'"
        )
        # Assistant messages (bot responses)
        await database.execute(
            "UPDATE messages SET message_type = 'assistant' "
            "WHERE sender IN ('bot', 'pynchy') AND message_type = 'user'"
        )
        # Everything else stays as 'user' (already the default)
        await database.commit()
    except Exception:
        pass


async def init_database() -> None:
    """Initialize the database connection and schema."""
    global _db
    db_path = STORE_DIR / "messages.db"
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


# --- Chat metadata ---


async def set_chat_cleared_at(chat_jid: str, timestamp: str) -> None:
    """Mark a chat as cleared at the given timestamp. Messages before this are hidden."""
    db = _get_db()
    await db.execute("UPDATE chats SET cleared_at = ? WHERE jid = ?", (timestamp, chat_jid))
    await db.commit()


async def store_chat_metadata(chat_jid: str, timestamp: str, name: str | None = None) -> None:
    """Store chat metadata only (no message content)."""
    db = _get_db()
    if name:
        await db.execute(
            """
            INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)
            ON CONFLICT(jid) DO UPDATE SET
                name = excluded.name,
                last_message_time = MAX(last_message_time, excluded.last_message_time)
            """,
            (chat_jid, name, timestamp),
        )
    else:
        await db.execute(
            """
            INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)
            ON CONFLICT(jid) DO UPDATE SET
                last_message_time = MAX(last_message_time, excluded.last_message_time)
            """,
            (chat_jid, chat_jid, timestamp),
        )
    await db.commit()


async def update_chat_name(chat_jid: str, name: str) -> None:
    """Update chat name without changing timestamp for existing chats."""
    db = _get_db()
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """
        INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)
        ON CONFLICT(jid) DO UPDATE SET name = excluded.name
        """,
        (chat_jid, name, now),
    )
    await db.commit()


async def get_all_chats() -> list[dict[str, str]]:
    """Get all known chats, ordered by most recent activity."""
    db = _get_db()
    cursor = await db.execute(
        "SELECT jid, name, last_message_time FROM chats ORDER BY last_message_time DESC"
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_last_group_sync() -> str | None:
    """Get timestamp of last group metadata sync."""
    db = _get_db()
    cursor = await db.execute("SELECT last_message_time FROM chats WHERE jid = '__group_sync__'")
    row = await cursor.fetchone()
    return row["last_message_time"] if row else None


async def set_last_group_sync() -> None:
    """Record that group metadata was synced."""
    db = _get_db()
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT OR REPLACE INTO chats (jid, name, last_message_time) "
        "VALUES ('__group_sync__', '__group_sync__', ?)",
        (now,),
    )
    await db.commit()


# --- Messages ---


async def store_message(msg: NewMessage, message_type: str = "user") -> None:
    """Store a message with full content.

    Args:
        msg: The message to store
        message_type: One of 'user', 'assistant', 'system', 'host', 'tool_result'
    """
    db = _get_db()
    await db.execute(
        "INSERT OR REPLACE INTO messages "
        "(id, chat_jid, sender, sender_name, content, timestamp, is_from_me, message_type, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            msg.id,
            msg.chat_jid,
            msg.sender,
            msg.sender_name,
            msg.content,
            msg.timestamp,
            1 if msg.is_from_me else 0,
            message_type,
            None,  # metadata - will be populated later if needed
        ),
    )
    await db.commit()


async def store_message_direct(
    *,
    id: str,
    chat_jid: str,
    sender: str,
    sender_name: str,
    content: str,
    timestamp: str,
    is_from_me: bool,
    message_type: str = "user",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Store a message directly (for non-WhatsApp channels).

    Args:
        message_type: One of 'user', 'assistant', 'system', 'host', 'tool_result'
        metadata: Optional metadata dict (e.g., severity, tool_use_id, etc.)
    """
    db = _get_db()
    metadata_json = json.dumps(metadata) if metadata else None
    await db.execute(
        "INSERT OR REPLACE INTO messages "
        "(id, chat_jid, sender, sender_name, content, timestamp, is_from_me, message_type, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            id,
            chat_jid,
            sender,
            sender_name,
            content,
            timestamp,
            1 if is_from_me else 0,
            message_type,
            metadata_json,
        ),
    )
    await db.commit()


async def get_new_messages(jids: list[str], last_timestamp: str) -> tuple[list[NewMessage], str]:
    """Get new messages across multiple groups since a timestamp."""
    if not jids:
        return [], last_timestamp

    db = _get_db()
    placeholders = ",".join("?" for _ in jids)
    sql = f"""
        SELECT id, chat_jid, sender, sender_name, content, timestamp, message_type, metadata
        FROM messages
        WHERE timestamp > ? AND chat_jid IN ({placeholders})
              AND (sender LIKE '%@%' OR sender IN ('tui-user', 'deploy'))
        ORDER BY timestamp
    """
    cursor = await db.execute(sql, [last_timestamp, *jids])
    rows = await cursor.fetchall()

    messages = []
    for row in rows:
        # Handle optional columns gracefully for backward compatibility
        try:
            message_type = row["message_type"] or "user"
        except (KeyError, IndexError):
            message_type = "user"

        try:
            metadata_str = row["metadata"]
            metadata = json.loads(metadata_str) if metadata_str else None
        except (KeyError, IndexError):
            metadata = None

        messages.append(NewMessage(
            id=row["id"],
            chat_jid=row["chat_jid"],
            sender=row["sender"],
            sender_name=row["sender_name"],
            content=row["content"],
            timestamp=row["timestamp"],
            message_type=message_type,
            metadata=metadata,
        ))


    new_timestamp = last_timestamp
    for msg in messages:
        if msg.timestamp > new_timestamp:
            new_timestamp = msg.timestamp

    return messages, new_timestamp


async def get_messages_since(chat_jid: str, since_timestamp: str) -> list[NewMessage]:
    """Get messages for a specific chat since a timestamp, excluding bot and host messages."""
    db = _get_db()
    sql = """
        SELECT id, chat_jid, sender, sender_name, content, timestamp, message_type, metadata
        FROM messages
        WHERE chat_jid = ? AND timestamp > ?
              AND (sender LIKE '%@%' OR sender IN ('tui-user', 'deploy'))
        ORDER BY timestamp
    """
    cursor = await db.execute(sql, (chat_jid, since_timestamp))
    rows = await cursor.fetchall()

    messages = []
    for row in rows:
        # Handle optional columns gracefully for backward compatibility
        try:
            message_type = row["message_type"] or "user"
        except (KeyError, IndexError):
            message_type = "user"

        try:
            metadata_str = row["metadata"]
            metadata = json.loads(metadata_str) if metadata_str else None
        except (KeyError, IndexError):
            metadata = None

        messages.append(NewMessage(
            id=row["id"],
            chat_jid=row["chat_jid"],
            sender=row["sender"],
            sender_name=row["sender_name"],
            content=row["content"],
            timestamp=row["timestamp"],
            message_type=message_type,
            metadata=metadata,
        ))
    return messages


async def get_chat_history(chat_jid: str, limit: int = 50) -> list[NewMessage]:
    """Get recent messages for a chat, including bot responses. Newest last.

    Respects the cleared_at boundary — messages before it are hidden.
    """
    db = _get_db()
    # Fetch cleared_at for this chat
    cleared_cursor = await db.execute("SELECT cleared_at FROM chats WHERE jid = ?", (chat_jid,))
    cleared_row = await cleared_cursor.fetchone()
    cleared_at = cleared_row["cleared_at"] if cleared_row and cleared_row["cleared_at"] else None

    if cleared_at:
        cursor = await db.execute(
            """
            SELECT id, chat_jid, sender, sender_name, content, timestamp, is_from_me,
                   message_type, metadata
            FROM messages
            WHERE chat_jid = ? AND timestamp > ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (chat_jid, cleared_at, limit),
        )
    else:
        cursor = await db.execute(
            """
            SELECT id, chat_jid, sender, sender_name, content, timestamp, is_from_me,
                   message_type, metadata
            FROM messages
            WHERE chat_jid = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (chat_jid, limit),
        )
    rows = await cursor.fetchall()

    messages = []
    for row in reversed(rows):
        # Handle optional columns gracefully for backward compatibility
        try:
            message_type = row["message_type"] or "user"
        except (KeyError, IndexError):
            message_type = "user"

        try:
            metadata_str = row["metadata"]
            metadata = json.loads(metadata_str) if metadata_str else None
        except (KeyError, IndexError):
            metadata = None

        messages.append(NewMessage(
            id=row["id"],
            chat_jid=row["chat_jid"],
            sender=row["sender"],
            sender_name=row["sender_name"],
            content=row["content"],
            timestamp=row["timestamp"],
            is_from_me=bool(row["is_from_me"]),
            message_type=message_type,
            metadata=metadata,
        ))
    return messages


# --- Scheduled tasks ---


async def create_task(task: dict[str, Any]) -> None:
    """Create a new scheduled task."""
    db = _get_db()
    await db.execute(
        """
        INSERT INTO scheduled_tasks
            (id, group_folder, chat_jid, prompt, schedule_type,
             schedule_value, context_mode, next_run, status, created_at,
             project_access)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task["id"],
            task["group_folder"],
            task["chat_jid"],
            task["prompt"],
            task["schedule_type"],
            task["schedule_value"],
            task.get("context_mode", "isolated"),
            task.get("next_run"),
            task["status"],
            task["created_at"],
            1 if task.get("project_access") else 0,
        ),
    )
    await db.commit()


async def get_task_by_id(task_id: str) -> ScheduledTask | None:
    """Get a task by its ID."""
    db = _get_db()
    cursor = await db.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,))
    row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_task(row)


async def get_tasks_for_group(group_folder: str) -> list[ScheduledTask]:
    """Get all tasks for a group, ordered by creation date."""
    db = _get_db()
    cursor = await db.execute(
        "SELECT * FROM scheduled_tasks WHERE group_folder = ? ORDER BY created_at DESC",
        (group_folder,),
    )
    rows = await cursor.fetchall()
    return [_row_to_task(row) for row in rows]


async def get_all_tasks() -> list[ScheduledTask]:
    """Get all tasks, ordered by creation date."""
    db = _get_db()
    cursor = await db.execute("SELECT * FROM scheduled_tasks ORDER BY created_at DESC")
    rows = await cursor.fetchall()
    return [_row_to_task(row) for row in rows]


async def update_task(task_id: str, updates: dict[str, Any]) -> None:
    """Update specific fields of a task."""
    allowed = {"prompt", "schedule_type", "schedule_value", "next_run", "status", "project_access"}
    fields: list[str] = []
    values: list[Any] = []

    for key, value in updates.items():
        if key in allowed:
            fields.append(f"{key} = ?")
            values.append(value)

    if not fields:
        return

    values.append(task_id)
    db = _get_db()
    await db.execute(
        f"UPDATE scheduled_tasks SET {', '.join(fields)} WHERE id = ?",
        values,
    )
    await db.commit()


async def delete_task(task_id: str) -> None:
    """Delete a task and its run logs."""
    db = _get_db()
    await db.execute("DELETE FROM task_run_logs WHERE task_id = ?", (task_id,))
    await db.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
    await db.commit()


async def get_active_task_for_group(group_folder: str) -> ScheduledTask | None:
    """Find the active scheduled task for a periodic agent group."""
    db = _get_db()
    cursor = await db.execute(
        "SELECT * FROM scheduled_tasks WHERE group_folder = ? AND status = 'active' LIMIT 1",
        (group_folder,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_task(row)


async def get_due_tasks() -> list[ScheduledTask]:
    """Get all active tasks that are due to run."""
    db = _get_db()
    now = datetime.now(UTC).isoformat()
    cursor = await db.execute(
        """
        SELECT * FROM scheduled_tasks
        WHERE status = 'active' AND next_run IS NOT NULL AND next_run <= ?
        ORDER BY next_run
        """,
        (now,),
    )
    rows = await cursor.fetchall()
    return [_row_to_task(row) for row in rows]


async def update_task_after_run(task_id: str, next_run: str | None, last_result: str) -> None:
    """Update a task after it has been run."""
    db = _get_db()
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """
        UPDATE scheduled_tasks
        SET next_run = ?, last_run = ?, last_result = ?,
            status = CASE WHEN ? IS NULL THEN 'completed' ELSE status END
        WHERE id = ?
        """,
        (next_run, now, last_result, next_run, task_id),
    )
    await db.commit()


async def log_task_run(log: TaskRunLog) -> None:
    """Log a task run."""
    db = _get_db()
    await db.execute(
        """
        INSERT INTO task_run_logs (task_id, run_at, duration_ms, status, result, error)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (log.task_id, log.run_at, log.duration_ms, log.status, log.result, log.error),
    )
    await db.commit()


# --- Router state ---


async def get_router_state(key: str) -> str | None:
    """Get a router state value."""
    db = _get_db()
    cursor = await db.execute("SELECT value FROM router_state WHERE key = ?", (key,))
    row = await cursor.fetchone()
    return row["value"] if row else None


async def set_router_state(key: str, value: str) -> None:
    """Set a router state value."""
    db = _get_db()
    await db.execute(
        "INSERT OR REPLACE INTO router_state (key, value) VALUES (?, ?)",
        (key, value),
    )
    await db.commit()


# --- Sessions ---


async def get_session(group_folder: str) -> str | None:
    """Get the session ID for a group."""
    db = _get_db()
    cursor = await db.execute(
        "SELECT session_id FROM sessions WHERE group_folder = ?", (group_folder,)
    )
    row = await cursor.fetchone()
    return row["session_id"] if row else None


async def set_session(group_folder: str, session_id: str) -> None:
    """Set the session ID for a group."""
    db = _get_db()
    await db.execute(
        "INSERT OR REPLACE INTO sessions (group_folder, session_id) VALUES (?, ?)",
        (group_folder, session_id),
    )
    await db.commit()


async def clear_session(group_folder: str) -> None:
    """Delete the session for a group, forcing a fresh session on next run."""
    db = _get_db()
    await db.execute("DELETE FROM sessions WHERE group_folder = ?", (group_folder,))
    await db.commit()


async def get_all_sessions() -> dict[str, str]:
    """Get all sessions as a dict of group_folder -> session_id."""
    db = _get_db()
    cursor = await db.execute("SELECT group_folder, session_id FROM sessions")
    rows = await cursor.fetchall()
    return {row["group_folder"]: row["session_id"] for row in rows}


# --- Registered groups ---


async def get_registered_group(jid: str) -> dict[str, Any] | None:
    """Get a registered group by JID. Returns dict with jid + RegisteredGroup fields."""
    db = _get_db()
    cursor = await db.execute("SELECT * FROM registered_groups WHERE jid = ?", (jid,))
    row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_registered_group(row)


async def set_registered_group(jid: str, group: RegisteredGroup) -> None:
    """Register or update a group."""
    db = _get_db()
    await db.execute(
        """INSERT OR REPLACE INTO registered_groups
            (jid, name, folder, trigger_pattern, added_at,
             container_config, requires_trigger)
         VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            jid,
            group.name,
            group.folder,
            group.trigger,
            group.added_at,
            json.dumps(asdict(group.container_config)) if group.container_config else None,
            1 if group.requires_trigger is None else (1 if group.requires_trigger else 0),
        ),
    )
    await db.commit()


async def get_all_registered_groups() -> dict[str, RegisteredGroup]:
    """Get all registered groups as dict of jid -> RegisteredGroup."""
    db = _get_db()
    cursor = await db.execute("SELECT * FROM registered_groups")
    rows = await cursor.fetchall()
    result: dict[str, RegisteredGroup] = {}
    for row in rows:
        entry = _row_to_registered_group(row)
        jid = entry.pop("jid")
        raw_cc = entry.get("container_config")
        result[jid] = RegisteredGroup(
            name=entry["name"],
            folder=entry["folder"],
            trigger=entry["trigger"],
            added_at=entry["added_at"],
            container_config=ContainerConfig.from_dict(raw_cc) if raw_cc else None,
            requires_trigger=entry.get("requires_trigger"),
        )
    return result


# --- JSON migration ---


async def _migrate_json_state() -> None:
    """Migrate state from legacy JSON files to SQLite."""

    def _read_and_archive(filename: str) -> Any | None:
        filepath = DATA_DIR / filename
        if not filepath.exists():
            return None
        try:
            data = json.loads(filepath.read_text())
            filepath.rename(filepath.with_suffix(filepath.suffix + ".migrated"))
            return data
        except Exception:
            return None

    # Migrate router_state.json
    router_state = _read_and_archive("router_state.json")
    if router_state:
        if router_state.get("last_timestamp"):
            await set_router_state("last_timestamp", router_state["last_timestamp"])
        if router_state.get("last_agent_timestamp"):
            await set_router_state(
                "last_agent_timestamp",
                json.dumps(router_state["last_agent_timestamp"]),
            )

    # Migrate sessions.json
    sessions = _read_and_archive("sessions.json")
    if sessions:
        for folder, session_id in sessions.items():
            await set_session(folder, session_id)

    # Migrate registered_groups.json
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


# --- Helpers ---


def _row_to_task(row: aiosqlite.Row) -> ScheduledTask:
    # project_access may not exist in old rows before migration
    try:
        project_access = bool(row["project_access"])
    except (IndexError, KeyError):
        project_access = False

    return ScheduledTask(
        id=row["id"],
        group_folder=row["group_folder"],
        chat_jid=row["chat_jid"],
        prompt=row["prompt"],
        schedule_type=row["schedule_type"],
        schedule_value=row["schedule_value"],
        context_mode=row["context_mode"] or "isolated",
        next_run=row["next_run"],
        last_run=row["last_run"],
        last_result=row["last_result"],
        status=row["status"],
        created_at=row["created_at"],
        project_access=project_access,
    )


def _row_to_registered_group(row: aiosqlite.Row) -> dict[str, Any]:
    container_config = None
    if row["container_config"]:
        container_config = json.loads(row["container_config"])

    requires_trigger = None if row["requires_trigger"] is None else row["requires_trigger"] == 1

    return {
        "jid": row["jid"],
        "name": row["name"],
        "folder": row["folder"],
        "trigger": row["trigger_pattern"],
        "added_at": row["added_at"],
        "container_config": container_config,
        "requires_trigger": requires_trigger,
    }
