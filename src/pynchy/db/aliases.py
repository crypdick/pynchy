"""JID alias CRUD — maps channel-specific JIDs to canonical workspace JIDs."""

from __future__ import annotations

from pynchy.db._connection import _get_db


async def set_jid_alias(alias_jid: str, canonical_jid: str, channel_name: str) -> None:
    """Create or update a JID alias."""
    db = _get_db()
    await db.execute(
        "INSERT OR REPLACE INTO jid_aliases"
        " (alias_jid, canonical_jid, channel_name) VALUES (?, ?, ?)",
        (alias_jid, canonical_jid, channel_name),
    )
    await db.commit()


async def get_canonical_jid(alias_jid: str) -> str | None:
    """Look up the canonical JID for an alias. Returns None if not found."""
    db = _get_db()
    cursor = await db.execute(
        "SELECT canonical_jid FROM jid_aliases WHERE alias_jid = ?",
        (alias_jid,),
    )
    row = await cursor.fetchone()
    return row["canonical_jid"] if row else None


async def get_aliases_for_jid(canonical_jid: str) -> dict[str, str]:
    """Get all aliases for a canonical JID. Returns {channel_name: alias_jid}."""
    db = _get_db()
    cursor = await db.execute(
        "SELECT channel_name, alias_jid FROM jid_aliases WHERE canonical_jid = ?",
        (canonical_jid,),
    )
    rows = await cursor.fetchall()
    return {row["channel_name"]: row["alias_jid"] for row in rows}


async def get_all_aliases() -> dict[str, str]:
    """Get all aliases as {alias_jid: canonical_jid}."""
    db = _get_db()
    cursor = await db.execute("SELECT alias_jid, canonical_jid FROM jid_aliases")
    rows = await cursor.fetchall()
    return {row["alias_jid"]: row["canonical_jid"] for row in rows}
