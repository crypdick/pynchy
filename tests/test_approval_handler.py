"""Tests for approval command handling in the chat pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import make_settings


@pytest.fixture
def ipc_dir(tmp_path: Path) -> Path:
    d = tmp_path / "ipc"
    d.mkdir()
    return d


@pytest.fixture
def settings(tmp_path: Path):
    return make_settings(data_dir=tmp_path)


class FakeDeps:
    """Minimal deps for testing approval handling."""

    def __init__(self):
        self.broadcast_messages: list[tuple[str, str]] = []

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None:
        self.broadcast_messages.append((chat_jid, text))


class TestHandleApprovalCommand:
    @pytest.mark.asyncio
    async def test_writes_decision_file_on_approve(self, ipc_dir: Path, settings):
        from pynchy.host.orchestrator.messaging.approval_handler import handle_approval_command
        from pynchy.host.container_manager.security.approval import create_pending_approval

        with (
            patch("pynchy.host.container_manager.security.approval.get_settings", return_value=settings),
        ):
            short_id = create_pending_approval("aabb001122334455", "x_post", "grp", "j@g.us", {"text": "hi"})
            deps = FakeDeps()
            await handle_approval_command(deps, "j@g.us", "approve", short_id, "testuser")

        decisions_dir = ipc_dir / "grp" / "approval_decisions"
        files = list(decisions_dir.glob("*.json"))
        assert len(files) == 1

        data = json.loads(files[0].read_text())
        assert data["approved"] is True
        assert data["decided_by"] == "testuser"
        assert data["request_id"] == "aabb001122334455"

    @pytest.mark.asyncio
    async def test_writes_decision_file_on_deny(self, ipc_dir: Path, settings):
        from pynchy.host.orchestrator.messaging.approval_handler import handle_approval_command
        from pynchy.host.container_manager.security.approval import create_pending_approval

        with (
            patch("pynchy.host.container_manager.security.approval.get_settings", return_value=settings),
        ):
            short_id = create_pending_approval("aabb001122334455", "x_post", "grp", "j@g.us", {"text": "hi"})
            deps = FakeDeps()
            await handle_approval_command(deps, "j@g.us", "deny", short_id, "testuser")

        decisions_dir = ipc_dir / "grp" / "approval_decisions"
        data = json.loads(list(decisions_dir.glob("*.json"))[0].read_text())
        assert data["approved"] is False

    @pytest.mark.asyncio
    async def test_unknown_id_sends_error(self, ipc_dir: Path, settings):
        from pynchy.host.orchestrator.messaging.approval_handler import handle_approval_command

        with patch("pynchy.host.container_manager.security.approval.get_settings", return_value=settings):
            deps = FakeDeps()
            await handle_approval_command(deps, "j@g.us", "approve", "nonexist", "testuser")

        assert len(deps.broadcast_messages) == 1
        assert "no pending" in deps.broadcast_messages[0][1].lower()

    @pytest.mark.asyncio
    async def test_confirmation_broadcast(self, ipc_dir: Path, settings):
        from pynchy.host.orchestrator.messaging.approval_handler import handle_approval_command
        from pynchy.host.container_manager.security.approval import create_pending_approval

        with (
            patch("pynchy.host.container_manager.security.approval.get_settings", return_value=settings),
        ):
            short_id = create_pending_approval("aabb001122334455", "x_post", "grp", "j@g.us", {})
            deps = FakeDeps()
            await handle_approval_command(deps, "j@g.us", "approve", short_id, "testuser")

        assert len(deps.broadcast_messages) == 1
        msg = deps.broadcast_messages[0][1]
        assert "Approved" in msg
        assert "x_post" in msg


class TestHandlePendingQuery:
    @pytest.mark.asyncio
    async def test_lists_pending_approvals(self, ipc_dir: Path, settings):
        from pynchy.host.orchestrator.messaging.approval_handler import handle_pending_query
        from pynchy.host.container_manager.security.approval import create_pending_approval

        with (
            patch("pynchy.host.container_manager.security.approval.get_settings", return_value=settings),
        ):
            create_pending_approval("req1", "x_post", "grp", "j@g.us", {})
            create_pending_approval("req2", "send_email", "grp", "j@g.us", {})
            deps = FakeDeps()
            await handle_pending_query(deps, "j@g.us")

        assert len(deps.broadcast_messages) == 1
        msg = deps.broadcast_messages[0][1]
        assert "x_post" in msg
        assert "send_email" in msg

    @pytest.mark.asyncio
    async def test_no_pending_shows_message(self, ipc_dir: Path, settings):
        from pynchy.host.orchestrator.messaging.approval_handler import handle_pending_query

        with patch("pynchy.host.container_manager.security.approval.get_settings", return_value=settings):
            deps = FakeDeps()
            await handle_pending_query(deps, "j@g.us")

        assert len(deps.broadcast_messages) == 1
        assert "no pending" in deps.broadcast_messages[0][1].lower()
