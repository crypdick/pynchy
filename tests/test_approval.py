"""Tests for the approval state manager."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import make_settings

from pynchy.db import _init_test_database


@pytest.fixture
async def _setup_db():
    await _init_test_database()


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
        from pynchy.security.approval import create_pending_approval

        with patch("pynchy.security.approval.get_settings", return_value=settings):
            create_pending_approval(
                request_id="aabb001122334455",
                tool_name="x_post",
                source_group="personal",
                chat_jid="group@g.us",
                request_data={"type": "service:x_post", "text": "hello"},
            )

        pending_dir = ipc_dir / "personal" / "pending_approvals"
        files = list(pending_dir.glob("*.json"))
        assert len(files) == 1
        assert files[0].name == "aabb001122334455.json"

        data = json.loads(files[0].read_text())
        assert data["request_id"] == "aabb001122334455"
        assert data["short_id"] == "aabb0011"
        assert data["tool_name"] == "x_post"
        assert data["source_group"] == "personal"
        assert data["chat_jid"] == "group@g.us"
        assert data["request_data"]["text"] == "hello"
        assert "timestamp" in data

    def test_atomic_write_no_tmp_left(self, ipc_dir: Path, settings):
        from pynchy.security.approval import create_pending_approval

        with patch("pynchy.security.approval.get_settings", return_value=settings):
            create_pending_approval(
                request_id="abc123",
                tool_name="test",
                source_group="grp",
                chat_jid="j@g.us",
                request_data={},
            )

        pending_dir = ipc_dir / "grp" / "pending_approvals"
        assert not list(pending_dir.glob("*.tmp"))


# -- list_pending_approvals ---------------------------------------------------


class TestListPendingApprovals:
    def test_lists_all_pending(self, ipc_dir: Path, settings):
        from pynchy.security.approval import (
            create_pending_approval,
            list_pending_approvals,
        )

        with patch("pynchy.security.approval.get_settings", return_value=settings):
            create_pending_approval("req1", "tool_a", "grp1", "j1@g.us", {})
            create_pending_approval("req2", "tool_b", "grp2", "j2@g.us", {})
            result = list_pending_approvals()

        assert len(result) == 2
        tool_names = {r["tool_name"] for r in result}
        assert tool_names == {"tool_a", "tool_b"}

    def test_filters_by_group(self, ipc_dir: Path, settings):
        from pynchy.security.approval import (
            create_pending_approval,
            list_pending_approvals,
        )

        with patch("pynchy.security.approval.get_settings", return_value=settings):
            create_pending_approval("req1", "tool_a", "grp1", "j1@g.us", {})
            create_pending_approval("req2", "tool_b", "grp2", "j2@g.us", {})
            result = list_pending_approvals(group="grp1")

        assert len(result) == 1
        assert result[0]["tool_name"] == "tool_a"

    def test_empty_when_no_pending(self, ipc_dir: Path, settings):
        from pynchy.security.approval import list_pending_approvals

        with patch("pynchy.security.approval.get_settings", return_value=settings):
            result = list_pending_approvals()

        assert result == []


# -- sweep_expired_approvals --------------------------------------------------


class TestSweepExpiredApprovals:
    @pytest.mark.asyncio
    async def test_expires_old_pending(self, _setup_db, ipc_dir: Path, settings):
        from pynchy.security.approval import (
            create_pending_approval,
            sweep_expired_approvals,
        )

        with patch("pynchy.security.approval.get_settings", return_value=settings):
            create_pending_approval("req-old", "tool_a", "grp", "j@g.us", {})

            # Backdate the file
            pending_file = ipc_dir / "grp" / "pending_approvals" / "req-old.json"
            data = json.loads(pending_file.read_text())
            data["timestamp"] = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
            pending_file.write_text(json.dumps(data))

            expired = await sweep_expired_approvals()

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
        from pynchy.security.approval import (
            create_pending_approval,
            sweep_expired_approvals,
        )

        with patch("pynchy.security.approval.get_settings", return_value=settings):
            create_pending_approval("req-fresh", "tool_b", "grp", "j@g.us", {})
            expired = await sweep_expired_approvals()

        assert len(expired) == 0
        assert (ipc_dir / "grp" / "pending_approvals" / "req-fresh.json").exists()

    @pytest.mark.asyncio
    async def test_cleans_orphaned_decisions(self, ipc_dir: Path, settings):
        from pynchy.security.approval import sweep_expired_approvals

        # Create decision with no matching pending
        decisions_dir = ipc_dir / "grp" / "approval_decisions"
        decisions_dir.mkdir(parents=True)
        orphan = decisions_dir / "orphan-req.json"
        orphan.write_text(json.dumps({"request_id": "orphan-req", "approved": True}))

        with patch("pynchy.security.approval.get_settings", return_value=settings):
            await sweep_expired_approvals()

        assert not orphan.exists()


# -- format_approval_notification ---------------------------------------------


class TestFormatApprovalNotification:
    def test_basic_format(self):
        from pynchy.security.approval import format_approval_notification

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

    def test_omits_internal_fields(self):
        from pynchy.security.approval import format_approval_notification

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
        from pynchy.security.approval import format_approval_notification

        long_text = "x" * 200
        msg = format_approval_notification(
            tool_name="tool",
            request_data={"body": long_text},
            short_id="abc12345",
        )
        assert "..." in msg
        assert long_text not in msg

    def test_empty_request_data(self):
        from pynchy.security.approval import format_approval_notification

        msg = format_approval_notification(
            tool_name="tool",
            request_data={},
            short_id="abc12345",
        )
        assert "no details" in msg.lower()
