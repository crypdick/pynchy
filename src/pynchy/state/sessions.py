"""Session tracking and router state (key-value store)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from pynchy.identifiers import (
    GroupFolder,
    SessionId,
)
from pynchy.state.connection import _get_db, atomic_write

# --- Router state ---

_CHAT_PAUSE_PREFIX = "chat_pause:"


async def get_router_state(key: str) -> str | None:
    """Get a router state value."""
    db = _get_db()
    cursor = await db.execute("SELECT value FROM router_state WHERE key = ?", (key,))
    row = await cursor.fetchone()
    return row["value"] if row else None


async def set_router_state(key: str, value: str) -> None:
    """Set a router state value."""
    async with atomic_write() as db:
        await db.execute(
            "INSERT OR REPLACE INTO router_state (key, value) VALUES (?, ?)",
            (key, value),
        )


async def pause_chat(chat_jid: str) -> None:
    """Persist a quiet fence until human input or a later recurring occurrence."""
    await set_router_state(f"{_CHAT_PAUSE_PREFIX}{chat_jid}", datetime.now(UTC).isoformat())


async def clear_chat_pause(chat_jid: str) -> None:
    """Remove one chat's durable quiet fence."""
    async with atomic_write() as db:
        await db.execute(
            "DELETE FROM router_state WHERE key = ?",
            (f"{_CHAT_PAUSE_PREFIX}{chat_jid}",),
        )


async def is_chat_paused(chat_jid: str) -> bool:
    """Return whether one chat has a durable quiet fence."""
    return await get_router_state(f"{_CHAT_PAUSE_PREFIX}{chat_jid}") is not None


async def save_router_state_batch(pairs: dict[str, str]) -> None:
    """Write multiple router state values in a single atomic transaction.

    All keys are written together — a crash can never leave them inconsistent.
    """
    async with atomic_write() as db:
        for key, value in pairs.items():
            await db.execute(
                "INSERT OR REPLACE INTO router_state (key, value) VALUES (?, ?)",
                (key, value),
            )


# --- Sessions ---


@dataclass(frozen=True, slots=True)
class SessionSecurityTaint:
    """Sticky security facts owned by one durable conversation session."""

    corruption_tainted: bool = False
    secret_tainted: bool = False


async def get_session(group_folder: GroupFolder) -> SessionId | None:
    """Get the session ID for a group."""
    db = _get_db()
    cursor = await db.execute(
        "SELECT session_id FROM sessions WHERE group_folder = ?", (group_folder,)
    )
    row = await cursor.fetchone()
    return SessionId(row["session_id"]) if row else None


async def set_session(group_folder: GroupFolder, session_id: SessionId) -> None:
    """Set the session ID for a group."""
    async with atomic_write() as db:
        await db.execute(
            "INSERT OR REPLACE INTO sessions (group_folder, session_id) VALUES (?, ?)",
            (group_folder, session_id),
        )


async def clear_session(group_folder: GroupFolder) -> None:
    """Delete a durable session and all security state scoped to that session."""
    async with atomic_write() as db:
        await db.execute("DELETE FROM sessions WHERE group_folder = ?", (group_folder,))
        await db.execute(
            "DELETE FROM session_security_taint WHERE group_folder = ?",
            (group_folder,),
        )


async def get_session_security_taint(group_folder: GroupFolder) -> SessionSecurityTaint:
    """Return sticky security taint for a durable session."""
    db = _get_db()
    cursor = await db.execute(
        """
        SELECT corruption_tainted, secret_tainted
        FROM session_security_taint
        WHERE group_folder = ?
        """,
        (group_folder,),
    )
    row = await cursor.fetchone()
    if row is None:
        return SessionSecurityTaint()
    return SessionSecurityTaint(
        corruption_tainted=bool(row["corruption_tainted"]),
        secret_tainted=bool(row["secret_tainted"]),
    )


async def mark_session_security_taint(
    group_folder: GroupFolder,
    *,
    corruption_tainted: bool = False,
    secret_tainted: bool = False,
) -> SessionSecurityTaint:
    """Atomically add sticky taint without allowing a continuation to clear it."""
    now = datetime.now(UTC).isoformat()
    async with atomic_write() as db:
        await db.execute(
            """
            INSERT INTO session_security_taint (
                group_folder, corruption_tainted, secret_tainted, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(group_folder) DO UPDATE SET
                corruption_tainted = MAX(
                    session_security_taint.corruption_tainted,
                    excluded.corruption_tainted
                ),
                secret_tainted = MAX(
                    session_security_taint.secret_tainted,
                    excluded.secret_tainted
                ),
                updated_at = excluded.updated_at
            """,
            (
                group_folder,
                int(corruption_tainted),
                int(secret_tainted),
                now,
            ),
        )
    return await get_session_security_taint(group_folder)


async def get_all_sessions() -> dict[str, str]:
    """Get all sessions as a dict of group_folder -> session_id."""
    db = _get_db()
    cursor = await db.execute("SELECT group_folder, session_id FROM sessions")
    rows = await cursor.fetchall()
    return {row["group_folder"]: row["session_id"] for row in rows}
