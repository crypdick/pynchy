"""Tests for the IPC protocol module — signal validation, parsing, construction.

The protocol defines the boundary between Tier 1 (signal-only) and Tier 2
(data-carrying) IPC. These tests verify that signals are correctly identified,
that malformed signals are rejected, and that the make_signal helper produces
valid payloads.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from pynchy.host.container_manager.ipc.protocol import (
    IPC_SCHEMA_VERSION,
    SIGNAL_TYPES,
    TIER2_TYPES,
    InboundChatMessage,
    IpcRequestEnvelope,
    make_ipc_request,
    make_signal,
    parse_ipc_file,
    parse_request_envelope,
    request_requires_idempotency_ledger,
    validate_signal,
)

if TYPE_CHECKING:
    from pathlib import Path

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
        data = {"type": "schedule_host_job", "command": "do stuff"}
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
        data = {"type": "schedule_host_job"}
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
            make_signal("schedule_host_job")

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
        f.write_text(json.dumps({"type": "schedule_host_job", "command": "hello"}))
        data = parse_ipc_file(f)
        assert data["type"] == "schedule_host_job"
        assert data["command"] == "hello"

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
# Canonical request envelope
# ---------------------------------------------------------------------------


class TestRequestEnvelope:
    """Tests for the canonical file-IPC request envelope."""

    def test_make_ipc_request_defaults_created_at(self):
        request = make_ipc_request(
            kind="refresh_groups",
            request_id="req-now",
            source_group="admin-1",
        )

        assert request["created_at"]

    def test_envelope_rejects_unsupported_schema_version(self):
        request = make_ipc_request(
            kind="refresh_groups",
            request_id="req-version",
            source_group="admin-1",
        )
        request["schema_version"] = IPC_SCHEMA_VERSION + 1

        with pytest.raises(ValueError, match="Unsupported IPC request"):
            IpcRequestEnvelope.from_dict(request)

    def test_envelope_rejects_unknown_request_kind(self):
        with pytest.raises(ValueError, match="Unknown IPC request kind"):
            make_ipc_request(
                kind="unknown-request",
                request_id="req-kind",
                source_group="admin-1",
            )

    def test_envelope_rejects_empty_required_string(self):
        request = make_ipc_request(
            kind="refresh_groups",
            request_id="req-required",
            source_group="admin-1",
        )
        request["request_id"] = ""

        with pytest.raises(ValueError, match="request_id must be a non-empty string"):
            IpcRequestEnvelope.from_dict(request)

    def test_envelope_rejects_non_string_required_field(self):
        request = make_ipc_request(
            kind="refresh_groups", request_id="req-required", source_group="admin-1"
        )
        request["source_group"] = 42

        with pytest.raises(ValueError, match="source_group must be a non-empty string"):
            IpcRequestEnvelope.from_dict(request)

    @pytest.mark.parametrize(
        "request_id",
        [chr(47) + "tmp/escape", "../escape", r"..\escape", "line\nbreak", "x" * 129],
    )
    def test_envelope_rejects_unsafe_request_id(self, request_id: str):
        request = make_ipc_request(
            kind="refresh_groups",
            request_id="safe-id",
            source_group="admin-1",
        )
        request["request_id"] = request_id

        with pytest.raises(ValueError, match="safe path component"):
            IpcRequestEnvelope.from_dict(request)

    def test_envelope_rejects_non_string_optional_field(self):
        request = make_ipc_request(
            kind="refresh_groups",
            request_id="req-optional",
            source_group="admin-1",
        )
        request["reply_to"] = 42

        with pytest.raises(ValueError, match="reply_to must be a string or null"):
            IpcRequestEnvelope.from_dict(request)

    def test_envelope_rejects_non_object_payload(self):
        request = make_ipc_request(
            kind="refresh_groups",
            request_id="req-payload",
            source_group="admin-1",
        )
        request["payload"] = []

        with pytest.raises(ValueError, match="payload must be an object"):
            IpcRequestEnvelope.from_dict(request)

    def test_make_ipc_request_writes_required_envelope_fields(self):
        """Every request file should carry the versioned transport envelope."""
        payload = {"jid": "new@g.us", "name": "New", "folder": "new", "trigger": "@pynchy"}

        request = make_ipc_request(
            kind="register_group",
            request_id="req-123",
            source_group="admin-1",
            created_at="2026-07-07T12:00:00+00:00",
            reply_to="responses",
            deadline="2026-07-07T12:05:00+00:00",
            payload=payload,
        )

        assert request == {
            "schema_version": IPC_SCHEMA_VERSION,
            "kind": "register_group",
            "request_id": "req-123",
            "source_group": "admin-1",
            "created_at": "2026-07-07T12:00:00+00:00",
            "reply_to": "responses",
            "deadline": "2026-07-07T12:05:00+00:00",
            "payload": payload,
        }

    def test_parse_request_envelope_returns_typed_envelope(self, tmp_path: Path):
        """Request files should parse once into a typed transport object."""
        file_path = tmp_path / "request.json"
        file_path.write_text(
            json.dumps(
                make_ipc_request(
                    kind="register_group",
                    request_id="req-register",
                    source_group="admin-1",
                    created_at="2026-07-07T12:00:00+00:00",
                    payload={
                        "jid": "new@g.us",
                        "name": "New",
                        "folder": "new",
                        "trigger": "@pynchy",
                    },
                )
            )
        )

        envelope = parse_request_envelope(file_path)

        assert isinstance(envelope, IpcRequestEnvelope)
        assert envelope.kind == "register_group"
        assert envelope.request_id == "req-register"
        assert envelope.source_group == "admin-1"
        assert envelope.payload["folder"] == "new"

    def test_parse_request_envelope_rejects_legacy_type_only_files(self, tmp_path: Path):
        """A requests/ file must use kind + envelope, not the old top-level type."""
        file_path = tmp_path / "legacy.json"
        file_path.write_text(
            json.dumps(
                {
                    "type": "register_group",
                    "jid": "new@g.us",
                    "name": "New",
                    "folder": "new",
                    "trigger": "@pynchy",
                }
            )
        )

        with pytest.raises(ValueError, match="kind"):
            parse_request_envelope(file_path)

    def test_host_mutating_requests_require_idempotency_ledger(self):
        """Host-mutating request kinds should be claimed before dispatch."""
        assert request_requires_idempotency_ledger("schedule_host_job") is True
        assert request_requires_idempotency_ledger("register_group") is True
        assert request_requires_idempotency_ledger("sync_worktree_to_main") is True
        assert request_requires_idempotency_ledger("publish_managed_feature") is True

    def test_read_only_requests_do_not_require_idempotency_ledger(self):
        """Read-only service calls can be replayed without mutating host state."""
        assert request_requires_idempotency_ledger("service:list_calendar") is False

    def test_all_static_request_kinds_parse_as_envelopes(self, tmp_path: Path):
        """Every registered static kind should use the typed request envelope."""
        for kind in sorted(SIGNAL_TYPES | TIER2_TYPES):
            file_path = tmp_path / f"{kind.replace(':', '_')}.json"
            file_path.write_text(
                json.dumps(
                    make_ipc_request(
                        kind=kind,
                        request_id=f"req-{kind.replace(':', '-')}",
                        source_group="admin-1",
                        created_at="2026-07-07T12:00:00+00:00",
                        payload={},
                    )
                )
            )

            envelope = parse_request_envelope(file_path)

            assert envelope.kind == kind


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

    def test_inbound_message_requires_chat_and_text(self):
        assert InboundChatMessage.from_dict({"type": "message", "chatJid": "chat"}) is None
