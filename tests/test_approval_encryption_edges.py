"""Public boundary coverage for encrypted approval state."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.fernet import Fernet

import pynchy.host.container_manager.security.approval as approval
from pynchy.plugins.api import OutboundEventType

if TYPE_CHECKING:
    from pathlib import Path


def _approval_root(tmp_path: Path) -> Path:
    return tmp_path / "approvals"


def _pending_file(root: Path, group: str, request_id: str) -> Path:
    return root / group / "pending_approvals" / f"{request_id}.json"


def _create_pending(tmp_path: Path, request_id: str = "request-1") -> Path:
    root = _approval_root(tmp_path)
    with patch.object(approval, "_approval_root", root):
        approval.create_pending_approval(
            request_id=request_id,
            tool_name="test_tool",
            source_group="group",
            approval_chat_jid="chat@g.us",
            request_data={"body": "secret"},
        )
    return _pending_file(root, "group", request_id)


class TestEncryptedPendingApproval:
    @pytest.mark.asyncio
    async def test_mcp_proxy_future_resolves_once(self):
        future = approval.register_mcp_proxy_approval("proxy-request")

        assert approval.resolve_mcp_proxy_approval("proxy-request", approved=True) is True
        assert await future is True
        assert approval.resolve_mcp_proxy_approval("proxy-request", approved=False) is False

    def test_plaintext_legacy_payload_is_rejected(self, tmp_path: Path):
        path = _pending_file(_approval_root(tmp_path), "group", "legacy")
        path.parent.mkdir(parents=True)
        payload = {"request_id": "legacy", "request_data": {"body": "old"}}
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ValueError, match="encrypted payload is missing"):
            approval.read_pending_approval(path)

    @pytest.mark.parametrize(
        ("encrypted_payload", "expected_error"),
        [(42, TypeError), ("not-a-fernet-token", ValueError)],
    )
    def test_rejects_invalid_encrypted_payload_types_and_tokens(
        self, tmp_path: Path, encrypted_payload: object, expected_error: type[Exception]
    ):
        path = _create_pending(tmp_path, "invalid-payload")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["encrypted_payload"] = encrypted_payload
        path.write_text(json.dumps(data), encoding="utf-8")

        with pytest.raises(expected_error, match="encrypted payload"):
            approval.read_pending_approval(path)

    def test_rejects_a_valid_token_containing_non_object_json(self, tmp_path: Path):
        path = _create_pending(tmp_path, "non-object-payload")
        root = _approval_root(tmp_path)
        data = json.loads(path.read_text(encoding="utf-8"))
        key = (root / "approval-payload.key").read_bytes()
        data["encrypted_payload"] = Fernet(key).encrypt(b"[]").decode()
        path.write_text(json.dumps(data), encoding="utf-8")

        with pytest.raises(TypeError, match="encrypted payload"):
            approval.read_pending_approval(path)

    def test_rejects_a_plaintext_non_object(self, tmp_path: Path):
        path = _pending_file(_approval_root(tmp_path), "group", "non-object")
        path.parent.mkdir(parents=True)
        path.write_text("[]", encoding="utf-8")

        with pytest.raises(TypeError, match="not an object"):
            approval.read_pending_approval(path)

    def test_unknown_redaction_marker_is_not_restored_as_secret_taint(self, tmp_path: Path):
        path = _create_pending(tmp_path, "unknown-marker")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["redaction_required"] = "unknown"
        path.write_text(json.dumps(data), encoding="utf-8")

        restored = approval.read_pending_approval(path)

        assert "secret_tainted" not in restored

    def test_key_creation_recovers_when_another_process_wins_race(self, tmp_path: Path):
        root = _approval_root(tmp_path)
        key = Fernet.generate_key()

        def another_process_wrote_key(_path: Path) -> int:
            (root / "approval-payload.key").write_bytes(key)
            raise FileExistsError

        with (
            patch.object(approval, "_approval_root", root),
            patch.object(approval, "_open_payload_key", side_effect=another_process_wrote_key),
        ):
            approval.create_pending_approval(
                "raced", "tool", "group", "chat@g.us", {"body": "payload"}
            )

        path = _pending_file(root, "group", "raced")
        assert approval.read_pending_approval(path)["request_data"] == {"body": "payload"}


class TestApprovalStateBoundaries:
    def test_state_root_requires_composition(self):
        with (
            patch.object(approval, "_approval_root", None),
            pytest.raises(RuntimeError, match="not been configured"),
        ):
            approval.approval_state_root()

    def test_list_skips_unreadable_encrypted_files(self, tmp_path: Path):
        path = _create_pending(tmp_path, "unreadable")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["encrypted_payload"] = "tampered"
        path.write_text(json.dumps(data), encoding="utf-8")

        with patch.object(approval, "_approval_root", _approval_root(tmp_path)):
            assert approval.list_pending_approvals() == []

    def test_list_skips_groups_without_pending_directories(self, tmp_path: Path):
        root = _approval_root(tmp_path)
        (root / "empty-group").mkdir(parents=True)

        with patch.object(approval, "_approval_root", root):
            assert approval.list_pending_approvals() == []

    def test_find_skips_unreadable_files_and_ignores_errors_group(self, tmp_path: Path):
        root = _approval_root(tmp_path)
        bad = _pending_file(root, "group", "bad")
        bad.parent.mkdir(parents=True)
        bad.write_text("not-json", encoding="utf-8")
        errors = _pending_file(root, "errors", "hidden")
        errors.parent.mkdir(parents=True)
        errors.write_text(json.dumps({"short_id": "target"}), encoding="utf-8")

        with patch.object(approval, "_approval_root", root):
            assert approval.find_pending_by_short_id("target") is None

    def test_find_skips_groups_without_pending_and_nonmatching_files(self, tmp_path: Path):
        root = _approval_root(tmp_path)
        (root / "empty-group").mkdir(parents=True)
        pending = _pending_file(root, "group", "other")
        pending.parent.mkdir(parents=True)
        pending.write_text(json.dumps({"short_id": "other"}), encoding="utf-8")

        with patch.object(approval, "_approval_root", root):
            assert approval.find_pending_by_short_id("target") is None

    def test_find_ignores_a_valid_nonmatching_pending_approval(self, tmp_path: Path):
        root = _approval_root(tmp_path)
        _create_pending(tmp_path, "other")

        with patch.object(approval, "_approval_root", root):
            assert approval.find_pending_by_short_id("target") is None

    def test_short_id_generation_handles_corrupt_files_and_exhaustion(self, tmp_path: Path):
        root = _approval_root(tmp_path)
        pending_dir = root / "group" / "pending_approvals"
        pending_dir.mkdir(parents=True)
        (pending_dir / "corrupt.json").write_text("not-json", encoding="utf-8")
        (pending_dir / "without-short-id.json").write_text("{}", encoding="utf-8")
        (pending_dir / "occupied.json").write_text(json.dumps({"short_id": "aa"}), encoding="utf-8")

        with (
            patch.object(approval, "_approval_root", root),
            patch.object(approval.secrets, "choice", return_value="a"),
        ):
            assert approval.generate_short_id("group") == "aaa"

    @pytest.mark.asyncio
    async def test_sweep_returns_empty_when_approval_root_does_not_exist(self, tmp_path: Path):
        expire = AsyncMock()
        with patch.object(approval, "_approval_root", _approval_root(tmp_path)):
            assert await approval.sweep_expired_approvals(expire) == []
        expire.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sweep_skips_malformed_pending_and_removes_only_orphan_decision(
        self, tmp_path: Path
    ):
        root = _approval_root(tmp_path)
        pending_dir = root / "group" / "pending_approvals"
        decisions_dir = root / "group" / "approval_decisions"
        pending_dir.mkdir(parents=True)
        decisions_dir.mkdir(parents=True)
        malformed = pending_dir / "malformed.json"
        malformed.write_text(json.dumps({"encrypted_payload": "bad"}), encoding="utf-8")
        retained = decisions_dir / "malformed.json"
        retained.write_text("{}", encoding="utf-8")
        orphan = decisions_dir / "orphan.json"
        orphan.write_text("{}", encoding="utf-8")

        with patch.object(approval, "_approval_root", root):
            assert await approval.sweep_expired_approvals(AsyncMock()) == []

        assert retained.exists()
        assert not orphan.exists()


class TestApprovalNotifications:
    @pytest.mark.parametrize("preface", [None, "Cop flagged this request"])
    def test_approval_event_contains_structured_metadata_and_optional_preface(
        self, preface: str | None
    ):
        event = approval.approval_event("test_tool", {"body": "payload"}, "a7", preface=preface)

        assert event.type is OutboundEventType.APPROVAL
        assert event.metadata == {"short_id": "a7", "tool_name": "test_tool"}
        assert "Approval required" in event.content
        if preface:
            assert event.content.startswith(preface)
        else:
            assert not event.content.startswith("Cop")
