"""Tests for the IPC protocol module — signal validation, parsing, construction.

The protocol defines the boundary between Tier 1 (signal-only) and Tier 2
(data-carrying) IPC. These tests verify that signals are correctly identified,
that malformed signals are rejected, and that the make_signal helper produces
valid payloads.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pynchy.ipc._protocol import (
    SIGNAL_TYPES,
    TIER2_TYPES,
    make_signal,
    parse_ipc_file,
    validate_signal,
)

# ---------------------------------------------------------------------------
# validate_signal — Tier 1 signal identification
# ---------------------------------------------------------------------------


class TestValidateSignal:
    """Tests for the validate_signal function."""

    def test_valid_signal_returns_type(self):
        """A well-formed signal payload should return the signal type."""
        data = {"signal": "refresh_groups"}
        assert validate_signal(data) == "refresh_groups"

    def test_signal_with_timestamp_is_valid(self):
        """Timestamp is allowed alongside the signal field."""
        data = {"signal": "refresh_groups", "timestamp": "2026-02-15T12:00:00"}
        assert validate_signal(data) == "refresh_groups"

    def test_no_signal_key_returns_none(self):
        """Data without a 'signal' key is not a signal (Tier 2 or legacy)."""
        data = {"type": "schedule_task", "prompt": "do stuff"}
        assert validate_signal(data) is None

    def test_signal_none_returns_none(self):
        """Explicit None signal value is not a signal."""
        data = {"signal": None}
        assert validate_signal(data) is None

    def test_unknown_signal_type_raises(self):
        """An unrecognized signal type should raise ValueError."""
        data = {"signal": "nonexistent_signal"}
        with pytest.raises(ValueError, match="Unknown signal type"):
            validate_signal(data)

    def test_signal_with_extra_payload_raises(self):
        """Signals must not carry payload keys beyond signal and timestamp."""
        data = {
            "signal": "refresh_groups",
            "extra_data": "should not be here",
        }
        with pytest.raises(ValueError, match="unexpected payload keys"):
            validate_signal(data)

    def test_empty_dict_returns_none(self):
        """An empty dict is not a signal."""
        assert validate_signal({}) is None

    def test_type_field_without_signal_returns_none(self):
        """A dict with 'type' but no 'signal' is not a signal (Tier 2 request)."""
        data = {"type": "schedule_task"}
        assert validate_signal(data) is None

    def test_all_registered_signal_types_are_valid(self):
        """Every type in SIGNAL_TYPES should pass validation."""
        for signal_type in SIGNAL_TYPES:
            data = {"signal": signal_type}
            assert validate_signal(data) == signal_type


# ---------------------------------------------------------------------------
# make_signal — Tier 1 signal construction
# ---------------------------------------------------------------------------


class TestMakeSignal:
    """Tests for the make_signal helper."""

    def test_creates_valid_signal(self):
        """make_signal should produce a dict that passes validate_signal."""
        payload = make_signal("refresh_groups")
        assert payload == {"signal": "refresh_groups"}
        assert validate_signal(payload) == "refresh_groups"

    def test_invalid_type_raises(self):
        """make_signal should reject non-signal types."""
        with pytest.raises(ValueError, match="Not a valid signal type"):
            make_signal("schedule_task")

    def test_all_signal_types(self):
        """make_signal should work for all registered signal types."""
        for signal_type in SIGNAL_TYPES:
            payload = make_signal(signal_type)
            assert validate_signal(payload) == signal_type


# ---------------------------------------------------------------------------
# parse_ipc_file — JSON file reading
# ---------------------------------------------------------------------------


class TestParseIpcFile:
    """Tests for parse_ipc_file."""

    def test_reads_valid_json(self, tmp_path: Path):
        """Should parse a well-formed JSON file."""
        f = tmp_path / "test.json"
        f.write_text(json.dumps({"type": "schedule_task", "prompt": "hello"}))
        data = parse_ipc_file(f)
        assert data["type"] == "schedule_task"
        assert data["prompt"] == "hello"

    def test_reads_signal_format(self, tmp_path: Path):
        """Should parse a signal-format file."""
        f = tmp_path / "signal.json"
        f.write_text(json.dumps({"signal": "refresh_groups"}))
        data = parse_ipc_file(f)
        assert validate_signal(data) == "refresh_groups"

    def test_invalid_json_raises(self, tmp_path: Path):
        """Should raise on malformed JSON."""
        f = tmp_path / "bad.json"
        f.write_text("not json {{{")
        with pytest.raises(json.JSONDecodeError):
            parse_ipc_file(f)

    def test_missing_file_raises(self, tmp_path: Path):
        """Should raise on missing file."""
        f = tmp_path / "nonexistent.json"
        with pytest.raises(FileNotFoundError):
            parse_ipc_file(f)


# ---------------------------------------------------------------------------
# Protocol invariants
# ---------------------------------------------------------------------------


class TestProtocolInvariants:
    """Tests for protocol-level invariants."""

    def test_signal_and_tier2_types_are_disjoint(self):
        """No type should appear in both SIGNAL_TYPES and TIER2_TYPES."""
        overlap = SIGNAL_TYPES & TIER2_TYPES
        assert overlap == set(), f"Types in both signal and tier2: {overlap}"

    def test_signal_types_is_frozen(self):
        """SIGNAL_TYPES should be immutable."""
        assert isinstance(SIGNAL_TYPES, frozenset)

    def test_tier2_types_is_frozen(self):
        """TIER2_TYPES should be immutable."""
        assert isinstance(TIER2_TYPES, frozenset)
