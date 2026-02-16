"""Session tracking and router state (key-value store)."""

from __future__ import annotations

from pynchy.db._connection import _get_db

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
