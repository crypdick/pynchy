"""SQLite FTS5 memory backend.

Stores memories in a dedicated ``data/memories.db`` database with BM25-ranked
full-text search and per-group isolation via ``group_folder`` column.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from pynchy.config import get_settings
from pynchy.logger import logger

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    group_folder TEXT NOT NULL,
    key TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'core',
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(group_folder, key)
);
CREATE INDEX IF NOT EXISTS idx_memories_group ON memories(group_folder);
CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(group_folder, category);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    key, content, content=memories, content_rowid=rowid
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, key, content) VALUES (new.rowid, new.key, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, key, content)
        VALUES('delete', old.rowid, old.key, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, key, content)
        VALUES('delete', old.rowid, old.key, old.content);
    INSERT INTO memories_fts(rowid, key, content) VALUES (new.rowid, new.key, new.content);
END;
"""


def _db_path() -> Path:
    return get_settings().data_dir / "memories.db"


class SqliteMemoryBackend:
    """SQLite FTS5 memory backend with BM25 ranked search."""

    name = "sqlite"

    def __init__(self) -> None:
        self._db: aiosqlite.Connection | None = None

    async def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            msg = "Memory backend not initialized — call init() first"
            raise RuntimeError(msg)
        return self._db

    async def init(self) -> None:
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        self._db = await aiosqlite.connect(str(path))
        self._db.row_factory = aiosqlite.Row

        # Connection tuning
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.execute("PRAGMA synchronous = NORMAL")
        await self._db.execute("PRAGMA mmap_size = 8388608")
        await self._db.execute("PRAGMA cache_size = -2000")
        await self._db.execute("PRAGMA temp_store = MEMORY")

        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        logger.info("Memory backend initialized", path=str(path))

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def save(
        self,
        group_folder: str,
        key: str,
        content: str,
        category: str = "core",
        metadata: dict | None = None,
    ) -> dict:
        db = await self._conn()
        now = datetime.now(tz=UTC).isoformat()
        meta_json = json.dumps(metadata or {})
        mem_id = uuid.uuid4().hex

        await db.execute(
            """INSERT INTO memories
            (id, group_folder, key, content, category, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_folder, key) DO UPDATE SET
                content = excluded.content,
                category = excluded.category,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at""",
            (mem_id, group_folder, key, content, category, meta_json, now, now),
        )
        await db.commit()

        # Determine if this was a create or update
        cursor = await db.execute(
            "SELECT id FROM memories WHERE group_folder = ? AND key = ?",
            (group_folder, key),
        )
        row = await cursor.fetchone()
        status = "created" if row and row["id"] == mem_id else "updated"

        return {"key": key, "status": status}

    async def recall(
        self,
        group_folder: str,
        query: str,
        category: str | None = None,
        limit: int = 5,
    ) -> list[dict]:
        if not query or not query.strip():
            return []

        db = await self._conn()

        # Tier 1: BM25 via FTS5
        results = await self._fts_search(db, group_folder, query, category, limit)

        # Tier 2: LIKE fallback if FTS5 returns nothing
        if not results:
            results = await self._like_search(db, group_folder, query, category, limit)

        return results

    async def _fts_search(
        self,
        db: aiosqlite.Connection,
        group_folder: str,
        query: str,
        category: str | None,
        limit: int,
    ) -> list[dict]:
        # Quote each word and join with OR for inclusive matching
        words = query.split()
        if not words:
            return []
        fts_query = " OR ".join(f'"{w}"' for w in words)

        if category:
            cursor = await db.execute(
                """SELECT m.key, m.content, m.category, m.metadata, m.updated_at,
                          bm25(memories_fts) as score
                   FROM memories m
                   JOIN memories_fts f ON m.rowid = f.rowid
                   WHERE memories_fts MATCH ? AND m.group_folder = ? AND m.category = ?
                   ORDER BY score
                   LIMIT ?""",
                (fts_query, group_folder, category, limit),
            )
        else:
            cursor = await db.execute(
                """SELECT m.key, m.content, m.category, m.metadata, m.updated_at,
                          bm25(memories_fts) as score
                   FROM memories m
                   JOIN memories_fts f ON m.rowid = f.rowid
                   WHERE memories_fts MATCH ? AND m.group_folder = ?
                   ORDER BY score
                   LIMIT ?""",
                (fts_query, group_folder, limit),
            )

        rows = await cursor.fetchall()
        return [
            {
                "key": r["key"],
                "content": r["content"],
                "category": r["category"],
                "metadata": json.loads(r["metadata"]),
                "updated_at": r["updated_at"],
                "score": -r["score"],  # Negate: BM25 returns negative scores
            }
            for r in rows
        ]

    async def _like_search(
        self,
        db: aiosqlite.Connection,
        group_folder: str,
        query: str,
        category: str | None,
        limit: int,
    ) -> list[dict]:
        like_pattern = f"%{query}%"

        if category:
            cursor = await db.execute(
                """SELECT key, content, category, metadata, updated_at
                   FROM memories
                   WHERE (content LIKE ? OR key LIKE ?)
                     AND group_folder = ? AND category = ?
                   ORDER BY updated_at DESC
                   LIMIT ?""",
                (like_pattern, like_pattern, group_folder, category, limit),
            )
        else:
            cursor = await db.execute(
                """SELECT key, content, category, metadata, updated_at
                   FROM memories
                   WHERE (content LIKE ? OR key LIKE ?)
                     AND group_folder = ?
                   ORDER BY updated_at DESC
                   LIMIT ?""",
                (like_pattern, like_pattern, group_folder, limit),
            )

        rows = await cursor.fetchall()
        return [
            {
                "key": r["key"],
                "content": r["content"],
                "category": r["category"],
                "metadata": json.loads(r["metadata"]),
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    async def forget(self, group_folder: str, key: str) -> dict:
        db = await self._conn()
        cursor = await db.execute(
            "DELETE FROM memories WHERE group_folder = ? AND key = ?",
            (group_folder, key),
        )
        await db.commit()
        return {"removed": cursor.rowcount > 0}

    async def list_keys(
        self,
        group_folder: str,
        category: str | None = None,
    ) -> list[dict]:
        db = await self._conn()

        if category:
            cursor = await db.execute(
                """SELECT key, category, updated_at FROM memories
                   WHERE group_folder = ? AND category = ?
                   ORDER BY updated_at DESC""",
                (group_folder, category),
            )
        else:
            cursor = await db.execute(
                """SELECT key, category, updated_at FROM memories
                   WHERE group_folder = ?
                   ORDER BY updated_at DESC""",
                (group_folder,),
            )

        rows = await cursor.fetchall()
        return [
            {"key": r["key"], "category": r["category"], "updated_at": r["updated_at"]}
            for r in rows
        ]
