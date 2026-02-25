"""End-to-end integration test for the human approval gate.

Exercises the full flow:
  service request (needs_human) → pending approval
  → chat approve/deny command → decision file
  → IPC handler executes → response file written
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import make_settings

from pynchy.config_models import ServiceTrustTomlConfig, WorkspaceSecurityTomlConfig
from pynchy.db import _init_test_database
from pynchy.ipc._handlers_service import _handle_service_request, clear_plugin_handler_cache
from pynchy.types import WorkspaceProfile


@pytest.fixture(autouse=True)
async def _setup():
    await _init_test_database()
    clear_plugin_handler_cache()


@pytest.fixture
def settings(tmp_path: Path):
    return make_settings(data_dir=tmp_path)


TEST_GROUP = WorkspaceProfile(
    jid="chat@g.us",
    name="Test",
    folder="mygroup",
    trigger="@Bot",
    added_at="2024-01-01",
)


class FakeDeps:
    """Minimal IpcDeps supporting both service handler and approval handler tests."""

    def __init__(self, groups: dict[str, WorkspaceProfile] | None = None):
        self._groups = groups or {}
        self.broadcast_messages: list[tuple[str, str]] = []

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return self._groups

    async def broadcast_to_channels(self, jid: str, text: str) -> None:
        self.broadcast_messages.append((jid, text))

    async def broadcast_host_message(self, jid: str, text: str) -> None:
        self.broadcast_messages.append((jid, text))


def _make_ws_settings(tmp_path: Path, tool_name: str, trust: ServiceTrustTomlConfig):
    """Build a Settings object with a workspace that has a specific tool trust config."""

    class FakeSettings:
        def __init__(self):
            from pynchy.config_models import WorkspaceConfig

            self.workspaces = {
                "mygroup": WorkspaceConfig(
                    name="Test",
                    security=WorkspaceSecurityTomlConfig(
                        services={tool_name: trust},
                    ),
                ),
            }
            self.services = {}
            self.data_dir = tmp_path

    return FakeSettings()


def _make_pm(*tool_names: str, handler_fn=None):
    """Create a fake plugin manager providing handlers for given tools."""

    async def _default(data: dict) -> dict:
        return {"result": {"status": "done", "tool": data.get("type")}}

    fn = handler_fn or _default
    pm = MagicMock()
    pm.hook.pynchy_service_handler.return_value = [
        {"tools": {name: fn for name in tool_names}},
    ]
    return pm


class TestApprovalE2E:
    """Full round-trip: request → block → approve → execute → response."""

    @pytest.mark.asyncio
    async def test_approve_happy_path(self, tmp_path: Path):
        """Service request with needs_human → approve → handler executes → response."""
        mock_handler = AsyncMock(return_value={"result": {"status": "posted"}})
        pm = _make_pm("x_post", handler_fn=mock_handler)

        ws_settings = _make_ws_settings(
            tmp_path,
            "x_post",
            ServiceTrustTomlConfig(
                public_source=False,
                secret_data=False,
                public_sink=False,
                dangerous_writes=True,  # triggers needs_human
            ),
        )
        approval_settings = make_settings(data_dir=tmp_path)

        deps = FakeDeps({"chat@g.us": TEST_GROUP})

        # Step 1: Service request hits needs_human — creates pending, broadcasts
        with (
            patch("pynchy.ipc._handlers_service.get_settings", return_value=ws_settings),
            patch("pynchy.ipc._handlers_service.get_plugin_manager", return_value=pm),
            patch("pynchy.security.approval.get_settings", return_value=approval_settings),
        ):
            data = {
                "type": "service:x_post",
                "request_id": "aabb001122334455",
                "text": "Hello world",
            }
            await _handle_service_request(data, "mygroup", False, deps)

        # Verify: no response file yet (container blocked)
        response_path = tmp_path / "ipc" / "mygroup" / "responses" / "aabb001122334455.json"
        assert not response_path.exists()

        # Verify: pending file created
        pending_path = tmp_path / "ipc" / "mygroup" / "pending_approvals" / "aabb001122334455.json"
        assert pending_path.exists()

        # Verify: notification broadcast
        assert len(deps.broadcast_messages) == 1
        assert "Approval required" in deps.broadcast_messages[0][1]
        assert "x_post" in deps.broadcast_messages[0][1]

        # Step 2: User sends "approve aabb0011" via chat
        from pynchy.chat.approval_handler import handle_approval_command

        with patch("pynchy.security.approval.get_settings", return_value=approval_settings):
            await handle_approval_command(deps, "chat@g.us", "approve", "aabb0011", "testuser")

        # Verify: decision file created
        decisions_dir = tmp_path / "ipc" / "mygroup" / "approval_decisions"
        decision_files = list(decisions_dir.glob("*.json"))
        assert len(decision_files) == 1
        decision = json.loads(decision_files[0].read_text())
        assert decision["approved"] is True

        # Verify: confirmation broadcast
        assert len(deps.broadcast_messages) == 2
        assert "Approved" in deps.broadcast_messages[1][1]

        # Step 3: IPC watcher picks up the decision file → executes handler
        from pynchy.ipc._handlers_approval import process_approval_decision

        # Need to re-register the plugin handlers for the approval handler
        clear_plugin_handler_cache()

        with (
            patch("pynchy.ipc._handlers_approval.get_settings", return_value=approval_settings),
            patch("pynchy.ipc._write.get_settings", return_value=approval_settings),
            patch(
                "pynchy.ipc._handlers_approval._get_plugin_handlers",
                return_value={"x_post": mock_handler},
            ),
        ):
            await process_approval_decision(decision_files[0], "mygroup")

        # Verify: handler was called with original request data
        mock_handler.assert_awaited_once()
        call_data = mock_handler.call_args[0][0]
        assert call_data["text"] == "Hello world"

        # Verify: response file now exists (container unblocked)
        assert response_path.exists()
        response = json.loads(response_path.read_text())
        assert response["result"]["status"] == "posted"

        # Verify: pending and decision files cleaned up
        assert not pending_path.exists()
        assert not decision_files[0].exists()

    @pytest.mark.asyncio
    async def test_deny_writes_error_response(self, tmp_path: Path):
        """Service request with needs_human → deny → error response → container unblocked."""
        pm = _make_pm("x_post")

        ws_settings = _make_ws_settings(
            tmp_path,
            "x_post",
            ServiceTrustTomlConfig(dangerous_writes=True),
        )
        approval_settings = make_settings(data_dir=tmp_path)

        deps = FakeDeps({"chat@g.us": TEST_GROUP})

        # Step 1: Service request hits needs_human
        with (
            patch("pynchy.ipc._handlers_service.get_settings", return_value=ws_settings),
            patch("pynchy.ipc._handlers_service.get_plugin_manager", return_value=pm),
            patch("pynchy.security.approval.get_settings", return_value=approval_settings),
        ):
            data = {
                "type": "service:x_post",
                "request_id": "ccdd556677889900",
                "text": "Bad tweet",
            }
            await _handle_service_request(data, "mygroup", False, deps)

        # Step 2: User denies
        from pynchy.chat.approval_handler import handle_approval_command

        with patch("pynchy.security.approval.get_settings", return_value=approval_settings):
            await handle_approval_command(deps, "chat@g.us", "deny", "ccdd5566", "testuser")

        # Step 3: IPC handler processes denial
        from pynchy.ipc._handlers_approval import process_approval_decision

        decisions_dir = tmp_path / "ipc" / "mygroup" / "approval_decisions"
        decision_files = list(decisions_dir.glob("*.json"))

        with (
            patch("pynchy.ipc._handlers_approval.get_settings", return_value=approval_settings),
            patch("pynchy.ipc._write.get_settings", return_value=approval_settings),
        ):
            await process_approval_decision(decision_files[0], "mygroup")

        # Verify: error response written
        response_path = tmp_path / "ipc" / "mygroup" / "responses" / "ccdd556677889900.json"
        assert response_path.exists()
        response = json.loads(response_path.read_text())
        assert "error" in response
        assert "denied" in response["error"].lower()

    @pytest.mark.asyncio
    async def test_safe_service_bypasses_approval(self, tmp_path: Path):
        """A fully safe service (all bools False) executes immediately without approval."""
        mock_handler = AsyncMock(return_value={"result": "ok"})
        pm = _make_pm("safe_tool", handler_fn=mock_handler)

        ws_settings = _make_ws_settings(
            tmp_path,
            "safe_tool",
            ServiceTrustTomlConfig(
                public_source=False,
                secret_data=False,
                public_sink=False,
                dangerous_writes=False,
            ),
        )

        deps = FakeDeps({"chat@g.us": TEST_GROUP})

        with (
            patch("pynchy.ipc._handlers_service.get_settings", return_value=ws_settings),
            patch("pynchy.ipc._write.get_settings", return_value=ws_settings),
            patch("pynchy.ipc._handlers_service.get_plugin_manager", return_value=pm),
        ):
            data = {
                "type": "service:safe_tool",
                "request_id": "safe-req-1",
            }
            await _handle_service_request(data, "mygroup", False, deps)

        # Handler called immediately
        mock_handler.assert_awaited_once()

        # Response written immediately (no approval needed)
        response_path = tmp_path / "ipc" / "mygroup" / "responses" / "safe-req-1.json"
        assert response_path.exists()

        # No pending approval created
        pending_dir = tmp_path / "ipc" / "mygroup" / "pending_approvals"
        assert not pending_dir.exists() or not list(pending_dir.glob("*.json"))

        # No notification broadcast
        assert len(deps.broadcast_messages) == 0
