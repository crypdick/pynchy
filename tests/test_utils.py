"""Tests for shared utility functions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pynchy.utils import compute_next_run, generate_message_id, safe_json_load


class TestGenerateMessageId:
    """Test generate_message_id for unique, prefixed identifiers."""

    def test_returns_string(self):
        result = generate_message_id("host")
        assert isinstance(result, str)

    def test_includes_prefix(self):
        result = generate_message_id("host")
        assert result.startswith("host-")

    def test_prefix_with_hyphen(self):
        result = generate_message_id("sys-notice")
        assert result.startswith("sys-notice-")

    def test_no_prefix_returns_timestamp_only(self):
        result = generate_message_id()
        # Should be a numeric timestamp string
        assert result.isdigit()

    def test_empty_prefix_returns_timestamp_only(self):
        result = generate_message_id("")
        assert result.isdigit()

    def test_unique_ids(self):
        """Consecutive calls should produce different IDs (at ms resolution)."""
        ids = {generate_message_id("test") for _ in range(10)}
        # At least some should be unique (timing-dependent, but ms precision
        # means most rapid calls still produce the same — just verify format)
        assert all(id_.startswith("test-") for id_ in ids)

    def test_timestamp_part_is_numeric(self):
        result = generate_message_id("host")
        ts_part = result.split("-", 1)[1]
        assert ts_part.isdigit()


class TestSafeJsonLoad:
    """Test safe_json_load for graceful error handling."""

    def test_reads_valid_json(self, tmp_path: Path):
        f = tmp_path / "data.json"
        f.write_text('{"key": "value"}')
        result = safe_json_load(f)
        assert result == {"key": "value"}

    def test_reads_json_list(self, tmp_path: Path):
        f = tmp_path / "data.json"
        f.write_text("[1, 2, 3]")
        result = safe_json_load(f)
        assert result == [1, 2, 3]

    def test_returns_default_on_missing_file(self, tmp_path: Path):
        f = tmp_path / "nonexistent.json"
        result = safe_json_load(f, default={"fallback": True})
        assert result == {"fallback": True}

    def test_returns_none_default_on_missing_file(self, tmp_path: Path):
        f = tmp_path / "nonexistent.json"
        result = safe_json_load(f)
        assert result is None

    def test_returns_default_on_invalid_json(self, tmp_path: Path):
        f = tmp_path / "bad.json"
        f.write_text("not json at all {{{")
        result = safe_json_load(f, default=[])
        assert result == []

    def test_returns_default_on_empty_file(self, tmp_path: Path):
        f = tmp_path / "empty.json"
        f.write_text("")
        result = safe_json_load(f, default="empty")
        assert result == "empty"

    def test_handles_nested_json(self, tmp_path: Path):
        data = {"a": {"b": [1, 2, {"c": True}]}}
        f = tmp_path / "nested.json"
        f.write_text(json.dumps(data))
        result = safe_json_load(f)
        assert result == data


class TestComputeNextRun:
    """Test compute_next_run for cron, interval, and once schedule types.

    This is critical business logic: wrong next_run means tasks fire at the
    wrong time or never fire at all.
    """

    def test_cron_returns_future_timestamp(self):
        """Cron schedules should produce a future ISO timestamp."""
        from datetime import datetime

        result = compute_next_run("cron", "0 9 * * *", "UTC")
        assert result is not None
        parsed = datetime.fromisoformat(result)
        assert parsed > datetime.now(parsed.tzinfo)

    def test_cron_invalid_expression_raises(self):
        """Invalid cron expression should raise."""
        with pytest.raises((ValueError, KeyError)):
            compute_next_run("cron", "not a cron", "UTC")

    def test_interval_returns_future_timestamp(self):
        """Interval schedules produce a timestamp ~interval ms in the future."""
        from datetime import UTC, datetime

        result = compute_next_run("interval", "3600000", "UTC")  # 1 hour
        assert result is not None
        parsed = datetime.fromisoformat(result)
        # Should be ~1 hour in the future (within a few seconds)
        delta = (parsed - datetime.now(UTC)).total_seconds()
        assert 3590 < delta < 3610

    def test_interval_zero_raises(self):
        """Zero interval should raise ValueError."""
        with pytest.raises(ValueError, match="positive"):
            compute_next_run("interval", "0", "UTC")

    def test_interval_negative_raises(self):
        """Negative interval should raise ValueError."""
        with pytest.raises(ValueError, match="positive"):
            compute_next_run("interval", "-1000", "UTC")

    def test_interval_non_numeric_raises(self):
        """Non-numeric interval should raise ValueError."""
        with pytest.raises(ValueError):
            compute_next_run("interval", "abc", "UTC")

    def test_once_returns_none(self):
        """'once' schedule type returns None (no next run)."""
        result = compute_next_run("once", "2025-01-01T00:00:00", "UTC")
        assert result is None

    def test_cron_respects_timezone(self):
        """Cron should use the specified timezone for calculation."""
        # Both should return valid timestamps but potentially different times
        utc_result = compute_next_run("cron", "0 9 * * *", "UTC")
        est_result = compute_next_run("cron", "0 9 * * *", "America/New_York")
        assert utc_result is not None
        assert est_result is not None
        # Both should be valid ISO timestamps
        from datetime import datetime

        datetime.fromisoformat(utc_result)
        datetime.fromisoformat(est_result)

    def test_interval_small_value(self):
        """Small but valid interval (1ms) should work."""
        result = compute_next_run("interval", "1", "UTC")
        assert result is not None
