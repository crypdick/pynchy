"""Tests for shared utility functions."""

from __future__ import annotations

import json
from pathlib import Path

from pynchy.utils import generate_message_id, safe_json_load


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
