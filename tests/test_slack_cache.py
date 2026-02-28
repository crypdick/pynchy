"""Tests for the Slack channel's TTL cache used for user/channel name resolution."""

from __future__ import annotations

import time
from unittest.mock import patch

from pynchy.plugins.channels.slack import _TtlCache


class TestTtlCache:
    def test_get_miss(self) -> None:
        cache = _TtlCache()
        assert cache.get("missing") is None

    def test_put_and_get(self) -> None:
        cache = _TtlCache()
        cache.put("user1", "Alice")
        assert cache.get("user1") == "Alice"

    def test_expired_entry_returns_none(self) -> None:
        cache = _TtlCache(ttl_seconds=0.01)
        cache.put("user1", "Alice")
        # Advance monotonic clock past TTL
        with patch.object(time, "monotonic", return_value=time.monotonic() + 1):
            assert cache.get("user1") is None

    def test_max_size_evicts_oldest(self) -> None:
        cache = _TtlCache(max_size=3)
        cache.put("a", "1")
        cache.put("b", "2")
        cache.put("c", "3")
        # Adding a 4th entry should evict "a" (oldest by insertion order)
        cache.put("d", "4")
        assert cache.get("a") is None
        assert cache.get("b") == "2"
        assert cache.get("d") == "4"

    def test_expired_entries_evicted_before_oldest(self) -> None:
        cache = _TtlCache(ttl_seconds=0.5, max_size=3)
        base = time.monotonic()
        with patch.object(time, "monotonic", return_value=base):
            cache.put("a", "1")
            cache.put("b", "2")
            cache.put("c", "3")

        # Fast-forward past TTL for a, b, c — then add d
        with patch.object(time, "monotonic", return_value=base + 1):
            cache.put("d", "4")
            # All expired entries should have been evicted, only d remains
            assert cache.get("a") is None
            assert cache.get("b") is None
            assert cache.get("c") is None
            assert cache.get("d") == "4"

    def test_overwrite_existing_key(self) -> None:
        cache = _TtlCache()
        cache.put("user1", "Alice")
        cache.put("user1", "Alice B")
        assert cache.get("user1") == "Alice B"
