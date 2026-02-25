"""Tests for the IPC approval decision handler."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from conftest import make_settings

from pynchy.db import _init_test_database


@pytest.fixture
async def _setup_db():
    await _init_test_database()


@pytest.fixture
def ipc_dir(tmp_path: Path) -> Path:
    d = tmp_path / "ipc"
    d.mkdir()
    return d


@pytest.fixture
def settings(tmp_path: Path):
    return make_settings(data_dir=tmp_path)


def _write_pending(
    ipc_dir: Path, group: str, request_id: str, tool_name: str, request_data: dict
) -> Path:
    """Helper to write a pending approval file."""
    pending_dir = ipc_dir / group / "pending_approvals"
    pending_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "request_id": request_id,
        "short_id": request_id[:8],
        "tool_name": tool_name,
        "source_group": group,
        "chat_jid": "j@g.us",
        "request_data": {
            "type": f"service:{tool_name}",
            "request_id": request_id,
            **request_data,
        },
        "timestamp": "2026-02-24T12:00:00+00:00",
    }
    filepath = pending_dir / f"{request_id}.json"
    filepath.write_text(json.dumps(data))
    return filepath


def _write_decision(ipc_dir: Path, group: str, request_id: str, *, approved: bool) -> Path:
    """Helper to write a decision file."""
    decisions_dir = ipc_dir / group / "approval_decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "request_id": request_id,
        "approved": approved,
        "decided_by": "testuser",
        "decided_at": "2026-02-24T12:01:00+00:00",
    }
    filepath = decisions_dir / f"{request_id}.json"
    filepath.write_text(json.dumps(data))
    return filepath


class TestProcessApprovalDecision:
    @pytest.mark.asyncio
    async def test_approved_executes_and_writes_response(self, _setup_db, ipc_dir: Path, settings):
        from pynchy.ipc._handlers_approval import process_approval_decision

        _write_pending(ipc_dir, "grp", "req123", "my_tool", {"arg": "val"})
        decision_file = _write_decision(ipc_dir, "grp", "req123", approved=True)

        mock_handler = AsyncMock(return_value={"result": {"status": "posted"}})

        with (
            patch("pynchy.ipc._handlers_approval.get_settings", return_value=settings),
            patch("pynchy.ipc._write.get_settings", return_value=settings),
            patch(
                "pynchy.ipc._handlers_approval._get_plugin_handlers",
                return_value={"my_tool": mock_handler},
            ),
        ):
            await process_approval_decision(decision_file, "grp")

        # Handler was called with original request data
        mock_handler.assert_awaited_once()
        call_data = mock_handler.call_args[0][0]
        assert call_data["arg"] == "val"

        # Response file written
        response_file = ipc_dir / "grp" / "responses" / "req123.json"
        assert response_file.exists()
        response = json.loads(response_file.read_text())
        assert response["result"]["status"] == "posted"

        # Pending and decision files cleaned up
        assert not (ipc_dir / "grp" / "pending_approvals" / "req123.json").exists()
        assert not decision_file.exists()

    @pytest.mark.asyncio
    async def test_denied_writes_error_response(self, _setup_db, ipc_dir: Path, settings):
        from pynchy.ipc._handlers_approval import process_approval_decision

        _write_pending(ipc_dir, "grp", "req456", "my_tool", {})
        decision_file = _write_decision(ipc_dir, "grp", "req456", approved=False)

        with (
            patch("pynchy.ipc._handlers_approval.get_settings", return_value=settings),
            patch("pynchy.ipc._write.get_settings", return_value=settings),
        ):
            await process_approval_decision(decision_file, "grp")

        response_file = ipc_dir / "grp" / "responses" / "req456.json"
        response = json.loads(response_file.read_text())
        assert "error" in response
        assert "denied" in response["error"].lower()

        # Cleaned up
        assert not (ipc_dir / "grp" / "pending_approvals" / "req456.json").exists()
        assert not decision_file.exists()

    @pytest.mark.asyncio
    async def test_missing_pending_cleans_decision(self, _setup_db, ipc_dir: Path, settings):
        """Decision with no matching pending file should be cleaned up."""
        from pynchy.ipc._handlers_approval import process_approval_decision

        decision_file = _write_decision(ipc_dir, "grp", "orphan", approved=True)

        with patch("pynchy.ipc._handlers_approval.get_settings", return_value=settings):
            await process_approval_decision(decision_file, "grp")

        assert not decision_file.exists()

    @pytest.mark.asyncio
    async def test_unknown_tool_writes_error(self, _setup_db, ipc_dir: Path, settings):
        """Approved request for unknown tool should write error response."""
        from pynchy.ipc._handlers_approval import process_approval_decision

        _write_pending(ipc_dir, "grp", "req789", "nonexistent_tool", {})
        decision_file = _write_decision(ipc_dir, "grp", "req789", approved=True)

        with (
            patch("pynchy.ipc._handlers_approval.get_settings", return_value=settings),
            patch("pynchy.ipc._write.get_settings", return_value=settings),
            patch(
                "pynchy.ipc._handlers_approval._get_plugin_handlers",
                return_value={},
            ),
        ):
            await process_approval_decision(decision_file, "grp")

        response_file = ipc_dir / "grp" / "responses" / "req789.json"
        response = json.loads(response_file.read_text())
        assert "error" in response

    @pytest.mark.asyncio
    async def test_handler_exception_writes_error(self, _setup_db, ipc_dir: Path, settings):
        """If the handler raises, write an error response instead of crashing."""
        from pynchy.ipc._handlers_approval import process_approval_decision

        _write_pending(ipc_dir, "grp", "reqfail", "bad_tool", {})
        decision_file = _write_decision(ipc_dir, "grp", "reqfail", approved=True)

        mock_handler = AsyncMock(side_effect=RuntimeError("boom"))

        with (
            patch("pynchy.ipc._handlers_approval.get_settings", return_value=settings),
            patch("pynchy.ipc._write.get_settings", return_value=settings),
            patch(
                "pynchy.ipc._handlers_approval._get_plugin_handlers",
                return_value={"bad_tool": mock_handler},
            ),
        ):
            await process_approval_decision(decision_file, "grp")

        response_file = ipc_dir / "grp" / "responses" / "reqfail.json"
        response = json.loads(response_file.read_text())
        assert "error" in response
        assert "boom" in response["error"]
