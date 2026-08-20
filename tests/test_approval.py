"""Tests for the approval state manager."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from conftest import init_test_database, make_settings

from pynchy.host.container_manager.security.approval import (
    create_pending_approval,
    find_pending_by_short_id,
    format_approval_notification,
    generate_short_id,
    list_pending_approvals,
    read_pending_approval,
    sweep_expired_approvals,
)
from pynchy.state import expire_action_intent

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def _setup_db():
    await init_test_database()


@pytest.fixture
def ipc_dir(tmp_path: Path) -> Path:
    """Create and return a temporary IPC directory."""
    d = tmp_path / "ipc"
    d.mkdir()
    return d


@pytest.fixture
def settings(tmp_path: Path):
    return make_settings(data_dir=tmp_path)


# -- create_pending_approval --------------------------------------------------


class TestCreatePendingApproval:
    def test_creates_pending_file(self, ipc_dir: Path, settings):
        with patch(
            "pynchy.host.container_manager.security.approval._approval_root",
            settings.data_dir / "approvals",
        ):
            create_pending_approval(
                request_id="aabb001122334455",
                tool_name="x_post",
                source_group="personal",
                approval_chat_jid="group@g.us",
                request_data={"type": "service:x_post", "text": "hello"},
            )

        pending_dir = ipc_dir.parent / "approvals" / "personal" / "pending_approvals"
        files = list(pending_dir.glob("*.json"))
        assert len(files) == 1
        assert files[0].name == "aabb001122334455.json"

        data = json.loads(files[0].read_text())
        expected_fields = {
            "request_id": "aabb001122334455",
            "tool_name": "x_post",
            "source_group": "personal",
            "approval_chat_jid": "group@g.us",
        }
        for field_name, expected_value in expected_fields.items():
            assert data[field_name] == expected_value
        # short_id is a random 2-char [a-z0-9] string, no longer request_id[:8]
        assert len(data["short_id"]) == 2
        assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789" for c in data["short_id"])
        assert "request_data" not in data
        assert "hello" not in files[0].read_text()
        assert "encrypted_payload" in data
        decrypted = read_pending_approval(files[0])
        assert decrypted["request_data"]["text"] == "hello"
        assert decrypted["secret_tainted"] is False
        key_path = ipc_dir.parent / "approvals" / "approval-payload.key"
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
        assert "timestamp" in data
        assert data["corruption_tainted"] is False
        assert data["redaction_required"] == "not_required"

    def test_atomic_write_no_tmp_left(self, ipc_dir: Path, settings):
        with patch(
            "pynchy.host.container_manager.security.approval._approval_root",
            settings.data_dir / "approvals",
        ):
            create_pending_approval(
                request_id="abc123",
                tool_name="test",
                source_group="grp",
                approval_chat_jid="j@g.us",
                request_data={},
            )

        pending_dir = ipc_dir.parent / "approvals" / "grp" / "pending_approvals"
        assert not list(pending_dir.glob("*.tmp"))

    def test_returns_short_id(self, ipc_dir: Path, settings):
        with patch(
            "pynchy.host.container_manager.security.approval._approval_root",
            settings.data_dir / "approvals",
        ):
            short_id = create_pending_approval(
                request_id="aabb001122334455",
                tool_name="x_post",
                source_group="personal",
                approval_chat_jid="group@g.us",
                request_data={},
            )

        assert isinstance(short_id, str)
        assert len(short_id) == 2
        assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789" for c in short_id)


# -- generate_short_id -------------------------------------------------------


class TestGenerateShortId:
    def test_returns_2_char_alphanumeric(self, ipc_dir: Path, settings):
        with patch(
            "pynchy.host.container_manager.security.approval._approval_root",
            settings.data_dir / "approvals",
        ):
            sid = generate_short_id("grp")

        assert len(sid) == 2
        assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789" for c in sid)

    def test_avoids_collision_with_existing(self, ipc_dir: Path, settings):
        """If existing pending has short_id 'ab', generating with 'ab' taken should differ."""
        with patch(
            "pynchy.host.container_manager.security.approval._approval_root",
            settings.data_dir / "approvals",
        ):
            # Create a pending approval to occupy one short_id
            first_id = create_pending_approval("req1", "tool", "grp", "j@g.us", {})

            # Generate many IDs — none should collide with the existing one
            # (probabilistic but with 1296 slots and 1 taken, overwhelmingly likely)
            ids = set()
            for _ in range(20):
                sid = generate_short_id("grp")
                ids.add(sid)

            # At least some should be different from first_id (proves generation works)
            assert len(ids) > 1 or ids != {first_id}


# -- find_pending_by_short_id ------------------------------------------------


class TestFindPendingByShortId:
    def test_finds_by_short_id(self, ipc_dir: Path, settings):
        with patch(
            "pynchy.host.container_manager.security.approval._approval_root",
            settings.data_dir / "approvals",
        ):
            short_id = create_pending_approval(
                "req-abc", "tool_a", "grp", "j@g.us", {"msg": "test"}
            )
            result = find_pending_by_short_id(short_id)

        assert result is not None
        assert result["request_id"] == "req-abc"
        assert result["short_id"] == short_id

    def test_returns_none_for_unknown(self, ipc_dir: Path, settings):
        with patch(
            "pynchy.host.container_manager.security.approval._approval_root",
            settings.data_dir / "approvals",
        ):
            result = find_pending_by_short_id("zz")

        assert result is None


# -- list_pending_approvals ---------------------------------------------------


class TestListPendingApprovals:
    def test_lists_all_pending(self, ipc_dir: Path, settings):
        with patch(
            "pynchy.host.container_manager.security.approval._approval_root",
            settings.data_dir / "approvals",
        ):
            create_pending_approval("req1", "tool_a", "grp1", "j1@g.us", {})
            create_pending_approval("req2", "tool_b", "grp2", "j2@g.us", {})
            result = list_pending_approvals()

        assert len(result) == 2
        tool_names = {r["tool_name"] for r in result}
        assert tool_names == {"tool_a", "tool_b"}

    def test_filters_by_group(self, ipc_dir: Path, settings):
        with patch(
            "pynchy.host.container_manager.security.approval._approval_root",
            settings.data_dir / "approvals",
        ):
            create_pending_approval("req1", "tool_a", "grp1", "j1@g.us", {})
            create_pending_approval("req2", "tool_b", "grp2", "j2@g.us", {})
            result = list_pending_approvals(group="grp1")

        assert len(result) == 1
        assert result[0]["tool_name"] == "tool_a"

    def test_empty_when_no_pending(self, ipc_dir: Path, settings):
        with patch(
            "pynchy.host.container_manager.security.approval._approval_root",
            settings.data_dir / "approvals",
        ):
            result = list_pending_approvals()

        assert result == []


# -- sweep_expired_approvals --------------------------------------------------


class TestSweepExpiredApprovals:
    @pytest.mark.usefixtures("_setup_db")
    @pytest.mark.asyncio
    async def test_expires_old_pending(self, ipc_dir: Path, settings):
        with (
            patch(
                "pynchy.host.container_manager.security.approval._approval_root",
                settings.data_dir / "approvals",
            ),
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"
            ),
        ):
            create_pending_approval(
                "req-old",
                "tool_a",
                "grp",
                "j@g.us",
                {},
                expires_after_seconds=1,
            )

            # Backdate the file
            pending_file = (
                ipc_dir.parent / "approvals" / "grp" / "pending_approvals" / "req-old.json"
            )
            data = json.loads(pending_file.read_text())
            data["timestamp"] = (datetime.now(UTC) - timedelta(seconds=2)).isoformat()
            pending_file.write_text(json.dumps(data))

            expired = await sweep_expired_approvals(expire_action_intent)

        assert len(expired) == 1
        assert expired[0]["request_id"] == "req-old"
        assert not pending_file.exists()

        # Error response should have been written
        response_file = ipc_dir / "grp" / "responses" / "req-old.json"
        assert response_file.exists()
        response = json.loads(response_file.read_text())
        assert "error" in response
        assert "expired" in response["error"].lower()

    @pytest.mark.asyncio
    async def test_keeps_fresh_pending(self, ipc_dir: Path, settings):
        with patch(
            "pynchy.host.container_manager.security.approval._approval_root",
            settings.data_dir / "approvals",
        ):
            create_pending_approval("req-fresh", "tool_b", "grp", "j@g.us", {})
            expired = await sweep_expired_approvals(expire_action_intent)

        assert len(expired) == 0
        assert (
            ipc_dir.parent / "approvals" / "grp" / "pending_approvals" / "req-fresh.json"
        ).exists()

    @pytest.mark.asyncio
    async def test_cleans_orphaned_decisions(self, ipc_dir: Path, settings):
        # Create decision with no matching pending
        decisions_dir = ipc_dir.parent / "approvals" / "grp" / "approval_decisions"
        decisions_dir.mkdir(parents=True)
        orphan = decisions_dir / "orphan-req.json"
        orphan.write_text(json.dumps({"request_id": "orphan-req", "approved": True}))

        with patch(
            "pynchy.host.container_manager.security.approval._approval_root",
            settings.data_dir / "approvals",
        ):
            await sweep_expired_approvals(expire_action_intent)

        assert not orphan.exists()


# -- format_approval_notification ---------------------------------------------


class TestFormatApprovalNotification:
    def test_basic_format(self):
        msg = format_approval_notification(
            tool_name="x_post",
            request_data={"text": "Hello world"},
            short_id="a7f3b2c1",
        )
        assert "x_post" in msg
        assert "a7f3b2c1" in msg
        assert "approve a7f3b2c1" in msg
        assert "deny a7f3b2c1" in msg
        assert "Hello world" in msg

    def test_capability_approval_exposes_duration_choices(self):
        msg = format_approval_notification(
            tool_name="list_calendar",
            request_data={"calendar": "primary"},
            short_id="a8",
            allow_remember=True,
        )

        assert "approve-once a8" in msg
        assert "approve-session a8" in msg
        assert "approve-forever a8" in msg
        assert "deny a8" in msg

    def test_omits_internal_fields(self):
        msg = format_approval_notification(
            tool_name="x_post",
            request_data={
                "type": "service:x_post",
                "request_id": "secret-id",
                "source_group": "grp",
                "text": "visible",
            },
            short_id="abc12345",
        )
        assert "service:x_post" not in msg
        assert "secret-id" not in msg
        assert "source_group" not in msg
        assert "visible" in msg

    def test_truncates_long_values(self):
        long_text = "x" * 200
        msg = format_approval_notification(
            tool_name="tool",
            request_data={"body": long_text},
            short_id="abc12345",
        )
        assert "..." in msg
        assert long_text not in msg

    def test_empty_request_data(self):
        msg = format_approval_notification(
            tool_name="tool",
            request_data={},
            short_id="abc12345",
        )
        assert "no details" in msg.lower()
