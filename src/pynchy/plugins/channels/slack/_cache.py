"""Bounded TTL cache for Slack API lookups (user/channel name resolution)."""

from __future__ import annotations

import time


class TtlCache:
    """Bounded cache with per-entry TTL for Slack API lookups.

    Evicts expired entries lazily on get/put.  Hard-caps at ``max_size``
    entries to bound memory regardless of TTL.
    """

    def __init__(self, ttl_seconds: float = 3600, max_size: int = 500) -> None:
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._data: dict[str, tuple[str, float]] = {}  # key → (value, expiry_mono)

    def get(self, key: str) -> str | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        value, expiry = entry
        if time.monotonic() > expiry:
            del self._data[key]
            return None
        return value

    def put(self, key: str, value: str) -> None:
        if len(self._data) >= self._max_size:
            self._evict_expired()
        # If still at capacity after eviction, drop oldest entry
        if len(self._data) >= self._max_size:
            oldest_key = next(iter(self._data))
            del self._data[oldest_key]
        self._data[key] = (value, time.monotonic() + self._ttl)

    def _evict_expired(self) -> None:
        now = time.monotonic()
        self._data = {k: v for k, v in self._data.items() if v[1] > now}
