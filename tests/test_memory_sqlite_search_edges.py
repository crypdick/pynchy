"""SQLite memory search fallback contracts."""

from __future__ import annotations

from asyncio import sleep
from typing import Any

from pynchy.plugins.memory.sqlite_memory.backend import SqliteMemoryBackend


async def test_recall_uses_category_like_fallback_when_fts_is_empty(tmp_path, monkeypatch) -> None:
    backend = SqliteMemoryBackend(tmp_path / "memories.db")
    await backend.init()
    try:
        await backend.save("group", "key", "needle", category="daily")

        async def no_fts_results(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            await sleep(0)
            return []

        monkeypatch.setattr(
            "pynchy.plugins.memory.sqlite_memory.backend.SqliteMemoryBackend._fts_search",
            no_fts_results,
        )
        results = await backend.recall("group", "needle", category="daily")

        assert [result["key"] for result in results] == ["key"]
    finally:
        await backend.close()
