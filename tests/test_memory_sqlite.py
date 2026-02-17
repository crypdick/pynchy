"""Tests for the SQLite FTS5 memory backend."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from pynchy.memory.plugins.sqlite_memory.backend import SqliteMemoryBackend


@pytest.fixture
async def backend(tmp_path):
    """Create an isolated backend using a temp directory."""
    with patch(
        "pynchy.memory.plugins.sqlite_memory.backend._db_path",
        return_value=tmp_path / "memories.db",
    ):
        b = SqliteMemoryBackend()
        await b.init()
        yield b
        await b.close()


class TestSave:
    async def test_save_creates_memory(self, backend):
        result = await backend.save("group-a", "fav-color", "blue")
        assert result == {"key": "fav-color", "status": "created"}

    async def test_save_upserts_existing(self, backend):
        await backend.save("group-a", "fav-color", "blue")
        result = await backend.save("group-a", "fav-color", "red")
        assert result == {"key": "fav-color", "status": "updated"}

    async def test_save_with_category_and_metadata(self, backend):
        result = await backend.save(
            "group-a",
            "project-deadline",
            "2026-03-01",
            category="daily",
            metadata={"source": "user"},
        )
        assert result["key"] == "project-deadline"
        assert result["status"] == "created"

    async def test_save_different_groups_same_key(self, backend):
        r1 = await backend.save("group-a", "name", "Alice")
        r2 = await backend.save("group-b", "name", "Bob")
        assert r1["status"] == "created"
        assert r2["status"] == "created"


class TestRecall:
    async def test_recall_finds_by_content(self, backend):
        await backend.save("group-a", "fav-color", "My favorite color is blue")
        results = await backend.recall("group-a", "blue")
        assert len(results) == 1
        assert results[0]["key"] == "fav-color"
        assert "blue" in results[0]["content"]

    async def test_recall_finds_by_key(self, backend):
        await backend.save("group-a", "favorite-color", "blue")
        results = await backend.recall("group-a", "favorite")
        assert len(results) >= 1
        assert results[0]["key"] == "favorite-color"

    async def test_recall_respects_group_isolation(self, backend):
        await backend.save("group-a", "secret", "alpha secret data")
        await backend.save("group-b", "info", "beta public data")
        results = await backend.recall("group-a", "data")
        assert all(r["key"] != "info" for r in results)

    async def test_recall_with_category_filter(self, backend):
        await backend.save("group-a", "k1", "daily note", category="daily")
        await backend.save("group-a", "k2", "core fact", category="core")
        results = await backend.recall("group-a", "note fact", category="daily")
        assert len(results) == 1
        assert results[0]["category"] == "daily"

    async def test_recall_with_limit(self, backend):
        for i in range(10):
            await backend.save("group-a", f"item-{i}", f"test content item number {i}")
        results = await backend.recall("group-a", "content", limit=3)
        assert len(results) == 3

    async def test_recall_returns_score_for_fts(self, backend):
        await backend.save("group-a", "k1", "the quick brown fox")
        results = await backend.recall("group-a", "fox")
        assert len(results) == 1
        assert "score" in results[0]
        assert results[0]["score"] > 0

    async def test_recall_like_fallback(self, backend):
        """LIKE fallback catches queries that FTS5 doesn't tokenize well."""
        await backend.save("group-a", "url-bookmark", "https://example.com/path")
        # FTS5 may not tokenize URLs well, but LIKE should catch it
        results = await backend.recall("group-a", "example.com")
        assert len(results) >= 1

    async def test_recall_empty_query(self, backend):
        await backend.save("group-a", "k1", "hello world")
        results = await backend.recall("group-a", "")
        assert results == []

    async def test_recall_no_matches(self, backend):
        await backend.save("group-a", "k1", "hello world")
        results = await backend.recall("group-a", "xyznonexistent")
        assert results == []

    async def test_recall_ranks_by_relevance(self, backend):
        await backend.save("group-a", "k1", "the color blue is nice")
        await backend.save("group-a", "k2", "blue blue blue everywhere blue")
        results = await backend.recall("group-a", "blue")
        assert len(results) == 2
        # k2 should rank higher (more blue occurrences)
        assert results[0]["key"] == "k2"


class TestForget:
    async def test_forget_removes_memory(self, backend):
        await backend.save("group-a", "fav-color", "blue")
        result = await backend.forget("group-a", "fav-color")
        assert result == {"removed": True}

        # Verify it's gone
        results = await backend.recall("group-a", "blue")
        assert len(results) == 0

    async def test_forget_nonexistent(self, backend):
        result = await backend.forget("group-a", "nonexistent")
        assert result == {"removed": False}

    async def test_forget_respects_group_isolation(self, backend):
        await backend.save("group-a", "shared-key", "alpha")
        await backend.save("group-b", "shared-key", "beta")
        await backend.forget("group-a", "shared-key")

        results_a = await backend.recall("group-a", "alpha")
        results_b = await backend.recall("group-b", "beta")
        assert len(results_a) == 0
        assert len(results_b) == 1


class TestListKeys:
    async def test_list_keys_returns_all(self, backend):
        await backend.save("group-a", "k1", "content1")
        await backend.save("group-a", "k2", "content2")
        keys = await backend.list_keys("group-a")
        assert len(keys) == 2
        key_names = [k["key"] for k in keys]
        assert "k1" in key_names
        assert "k2" in key_names

    async def test_list_keys_with_category_filter(self, backend):
        await backend.save("group-a", "k1", "c1", category="core")
        await backend.save("group-a", "k2", "c2", category="daily")
        keys = await backend.list_keys("group-a", category="core")
        assert len(keys) == 1
        assert keys[0]["key"] == "k1"

    async def test_list_keys_respects_group_isolation(self, backend):
        await backend.save("group-a", "k1", "c1")
        await backend.save("group-b", "k2", "c2")
        keys = await backend.list_keys("group-a")
        assert len(keys) == 1
        assert keys[0]["key"] == "k1"

    async def test_list_keys_empty(self, backend):
        keys = await backend.list_keys("group-a")
        assert keys == []

    async def test_list_keys_ordered_by_updated_at(self, backend):
        await backend.save("group-a", "old", "old content")
        await backend.save("group-a", "new", "new content")
        keys = await backend.list_keys("group-a")
        assert keys[0]["key"] == "new"  # Most recent first


class TestLifecycle:
    async def test_init_creates_db(self, tmp_path):
        db_path = tmp_path / "test.db"
        with patch(
            "pynchy.memory.plugins.sqlite_memory.backend._db_path",
            return_value=db_path,
        ):
            b = SqliteMemoryBackend()
            await b.init()
            assert db_path.exists()
            await b.close()

    async def test_operations_fail_without_init(self):
        b = SqliteMemoryBackend()
        with pytest.raises(RuntimeError, match="not initialized"):
            await b.save("g", "k", "c")

    async def test_close_is_idempotent(self, backend):
        await backend.close()
        await backend.close()  # Should not raise
