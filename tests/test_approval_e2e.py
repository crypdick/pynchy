"""End-to-end integration test for the human approval gate.

Exercises the full flow:
  service request (needs_human) → pending approval
  → chat approve/deny command → decision file
  → IPC handler executes → response file written
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from conftest import NullIpcDeps, make_host_action_catalog, make_settings

import pynchy.host.container_manager.ipc.handlers_approval as handlers_approval
import pynchy.host.container_manager.ipc.registry as registry
from pynchy import state
from pynchy.atomic_json import write_json_atomic
from pynchy.host.container_manager.ipc.handlers_approval import process_approval_decision
from pynchy.host.container_manager.ipc.handlers_service import clear_plugin_handler_cache
from pynchy.host.container_manager.security.approval import (
    approval_state_root,
    find_pending_by_short_id,
    list_pending_approvals,
)
from pynchy.host.container_manager.security.gate import create_gate, destroy_gate
from pynchy.host.orchestrator.messaging.approval_handler import handle_approval_command
from pynchy.host.orchestrator.messaging.deps import ApprovalRuntimeOperations
from pynchy.plugins.api import OutboundEventType
from pynchy.workspace.api import (
    ServiceTrustConfig,
    WorkspaceProfile,
    WorkspaceSecurity,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
async def _setup():
    await state.init_test_database()
    clear_plugin_handler_cache()
    yield
    destroy_gate("mygroup", 1000.0)


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


class FakeDeps(NullIpcDeps):
    """Minimal IpcDeps supporting both service handler and approval handler tests."""

    def __init__(self, groups: dict[str, WorkspaceProfile] | None = None):
        self._groups = groups or {}
        self.broadcast_messages: list[tuple[str, str]] = []
        self.broadcast_events: list[object] = []
        self.approval_runtime_operations = ApprovalRuntimeOperations(
            find_pending_by_short_id=find_pending_by_short_id,
            list_pending_approvals=list_pending_approvals,
            persist_and_process=self._persist_and_process_approval,
        )

    async def _persist_and_process_approval(
        self, source_group: str, decision_data: dict[str, object]
    ) -> None:
        decisions_dir = approval_state_root() / source_group / "approval_decisions"
        decisions_dir.mkdir(parents=True, exist_ok=True)
        decision_file = decisions_dir / f"{decision_data['request_id']}.json"
        write_json_atomic(decision_file, decision_data, indent=2)
        await handlers_approval.process_approval_decision(decision_file, source_group, deps=self)

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return self._groups

    async def broadcast_to_channels(self, jid: str, event: object) -> None:
        self.broadcast_events.append(event)
        # event is an OutboundEvent; extract .content for string assertions
        content = event.content if hasattr(event, "content") else str(event)
        self.broadcast_messages.append((jid, content))

    async def broadcast_host_message(self, jid: str, text: str) -> None:
        self.broadcast_messages.append((jid, text))


def _register_gate(
    source_group: str,
    tool_name: str,
    trust: ServiceTrustConfig,
) -> None:
    """Register a SecurityGate with the given trust config for a tool."""
    security = WorkspaceSecurity(services={tool_name: trust})
    create_gate(source_group, 1000.0, security)


def _make_ws_settings(tmp_path: Path):
    """Build a minimal Settings object (security is now via SecurityGate)."""

    class FakeSettings:
        def __init__(self):
            self.workspaces = {}
            self.services = {}
            self.tools = {}
            self.data_dir = tmp_path

    return FakeSettings()


def _make_catalog(*tool_names: str, handler_fn=None):
    """Create a typed catalog for synthetic approval tools."""

    async def _default(data: dict):
        return await asyncio.sleep(
            0,
            result={"result": {"status": "done", "tool": data.get("type")}},
        )

    return make_host_action_catalog(*tool_names, handler=handler_fn or _default)


def _response_path(tmp_path: Path, request_id: str) -> Path:
    return tmp_path / "ipc" / "mygroup" / "responses" / f"{request_id}.json"


def _pending_path(tmp_path: Path, request_id: str) -> Path:
    return tmp_path / "approvals" / "mygroup" / "pending_approvals" / f"{request_id}.json"


class TestApprovalE2E:
    """Full round-trip: request → block → approve → execute → response."""

    @staticmethod
    def _assert_broadcast(deps: FakeDeps, index: int, *snippets: str) -> None:
        message = deps.broadcast_messages[index][1]
        for snippet in snippets:
            assert snippet in message

    @staticmethod
    def _short_id_from_pending(pending_path: Path) -> str:
        pending_data = json.loads(pending_path.read_text(encoding="utf-8"))
        return pending_data["short_id"]

    @staticmethod
    def _approved_decision(tmp_path: Path) -> tuple[Path, dict[str, object]]:
        decisions_dir = tmp_path / "approvals" / "mygroup" / "approval_decisions"
        decision_files = list(decisions_dir.glob("*.json"))
        assert len(decision_files) == 1
        decision = json.loads(decision_files[0].read_text(encoding="utf-8"))
        assert decision["approved"] is True
        return decision_files[0], decision

    @staticmethod
    def _assert_approved_response(
        *,
        mock_handler: AsyncMock,
        response_path: Path,
        pending_path: Path,
        decision_file: Path,
    ) -> None:
        mock_handler.assert_awaited_once()
        call_data = mock_handler.call_args[0][0]
        assert call_data["text"] == "Hello world"
        assert response_path.exists()
        response = json.loads(response_path.read_text(encoding="utf-8"))
        assert response["result"]["status"] == "posted"
        assert not pending_path.exists()
        assert not decision_file.exists()

    async def _dispatch_service_request(
        self,
        *,
        deps: FakeDeps,
        data: dict[str, str],
        catalog,
        ws_settings,
        approval_settings,
    ) -> None:
        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_service.get_settings",
                return_value=ws_settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
                return_value=catalog,
            ),
            patch(
                "pynchy.host.container_manager.security.approval._approval_root",
                approval_settings.data_dir / "approvals",
            ),
        ):
            await registry.dispatch(data, "mygroup", False, deps)

    async def _approve_request(
        self,
        *,
        deps: FakeDeps,
        approval_settings,
        short_id: str,
    ) -> None:
        with (
            patch(
                "pynchy.host.container_manager.security.approval._approval_root",
                approval_settings.data_dir / "approvals",
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.process_approval_decision",
                new_callable=AsyncMock,
            ),
        ):
            await handle_approval_command(deps, "chat@g.us", "approve", short_id, "testuser")

    async def _process_approved_request(
        self,
        *,
        decision_file: Path,
        approval_settings,
        mock_handler: AsyncMock,
        deps: FakeDeps,
    ) -> None:
        clear_plugin_handler_cache()
        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
                return_value=approval_settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir",
                approval_settings.data_dir / "ipc",
            ),
            patch(
                "pynchy.host.container_manager.ipc.approval_decision_context._get_action_catalog",
                return_value=make_host_action_catalog("x_post", handler=mock_handler),
            ),
        ):
            await process_approval_decision(decision_file, "mygroup", deps=deps)

    @pytest.mark.asyncio
    async def test_approve_happy_path(self, tmp_path: Path):
        """Service request with needs_human → approve → handler executes → response."""
        mock_handler = AsyncMock(return_value={"result": {"status": "posted"}})
        catalog = _make_catalog("x_post", handler_fn=mock_handler)

        _register_gate(
            "mygroup",
            "x_post",
            ServiceTrustConfig(
                public_source=False,
                secret_data=False,
                public_sink=False,
                dangerous_writes=True,  # triggers needs_human
            ),
        )
        ws_settings = _make_ws_settings(tmp_path)
        approval_settings = make_settings(data_dir=tmp_path)

        deps = FakeDeps({"chat@g.us": TEST_GROUP})
        request_id = "aabb001122334455"
        data = {
            "type": "service:x_post",
            "request_id": request_id,
            "text": "Hello world",
        }
        response_path = _response_path(tmp_path, request_id)
        pending_path = _pending_path(tmp_path, request_id)

        # Step 1: Service request hits needs_human — creates pending, broadcasts
        await self._dispatch_service_request(
            deps=deps,
            data=data,
            catalog=catalog,
            ws_settings=ws_settings,
            approval_settings=approval_settings,
        )

        # Verify: no response file yet (container blocked)
        assert not response_path.exists()

        # Verify: pending file created
        assert pending_path.exists()

        # Verify: notification broadcast
        assert len(deps.broadcast_messages) == 1
        self._assert_broadcast(deps, 0, "Approval required", "x_post")
        approval_event = deps.broadcast_events[0]
        assert approval_event.type is OutboundEventType.APPROVAL
        assert approval_event.metadata["short_id"]

        # Read the actual short_id from the pending file (now random 2-char)
        short_id = self._short_id_from_pending(pending_path)

        # Step 2: User sends "approve <short_id>" via chat
        await self._approve_request(
            deps=deps,
            approval_settings=approval_settings,
            short_id=short_id,
        )

        # Verify: decision file created
        decision_file, _ = self._approved_decision(tmp_path)

        # Verify: confirmation broadcast
        assert len(deps.broadcast_messages) == 2
        self._assert_broadcast(deps, 1, "Approved")

        # Step 3: IPC watcher picks up the decision file → executes handler
        await self._process_approved_request(
            decision_file=decision_file,
            approval_settings=approval_settings,
            mock_handler=mock_handler,
            deps=deps,
        )

        self._assert_approved_response(
            mock_handler=mock_handler,
            response_path=response_path,
            pending_path=pending_path,
            decision_file=decision_file,
        )

    @pytest.mark.asyncio
    async def test_deny_writes_error_response(self, tmp_path: Path):
        """Service request with needs_human → deny → error response → container unblocked."""
        catalog = _make_catalog("x_post")

        _register_gate(
            "mygroup",
            "x_post",
            ServiceTrustConfig(dangerous_writes=True),
        )
        ws_settings = _make_ws_settings(tmp_path)
        approval_settings = make_settings(data_dir=tmp_path)

        deps = FakeDeps({"chat@g.us": TEST_GROUP})

        # Step 1: Service request hits needs_human
        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_service.get_settings",
                return_value=ws_settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
                return_value=catalog,
            ),
            patch(
                "pynchy.host.container_manager.security.approval._approval_root",
                approval_settings.data_dir / "approvals",
            ),
        ):
            data = {
                "type": "service:x_post",
                "request_id": "ccdd556677889900",
                "text": "Bad tweet",
            }
            await registry.dispatch(data, "mygroup", False, deps)

        # Read the actual short_id from the pending file
        pending_path = (
            tmp_path / "approvals" / "mygroup" / "pending_approvals" / "ccdd556677889900.json"
        )
        pending_data = json.loads(pending_path.read_text(encoding="utf-8"))
        short_id = pending_data["short_id"]

        # Step 2: User denies
        with (
            patch(
                "pynchy.host.container_manager.security.approval._approval_root",
                approval_settings.data_dir / "approvals",
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.process_approval_decision",
                new_callable=AsyncMock,
            ),
        ):
            await handle_approval_command(deps, "chat@g.us", "deny", short_id, "testuser")

        # Step 3: IPC handler processes denial
        decisions_dir = tmp_path / "approvals" / "mygroup" / "approval_decisions"
        decision_files = list(decisions_dir.glob("*.json"))

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
                return_value=approval_settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir",
                approval_settings.data_dir / "ipc",
            ),
        ):
            await process_approval_decision(decision_files[0], "mygroup")

        # Verify: error response written
        response_path = tmp_path / "ipc" / "mygroup" / "responses" / "ccdd556677889900.json"
        assert response_path.exists()
        response = json.loads(response_path.read_text(encoding="utf-8"))
        assert "error" in response
        assert "denied" in response["error"].lower()

    @pytest.mark.asyncio
    async def test_safe_service_bypasses_approval(self, tmp_path: Path):
        """A fully safe service (all bools False) executes immediately without approval."""
        mock_handler = AsyncMock(return_value={"result": "ok"})
        catalog = _make_catalog("safe_tool", handler_fn=mock_handler)

        _register_gate(
            "mygroup",
            "safe_tool",
            ServiceTrustConfig(
                public_source=False,
                secret_data=False,
                public_sink=False,
                dangerous_writes=False,
            ),
        )
        ws_settings = _make_ws_settings(tmp_path)

        deps = FakeDeps({"chat@g.us": TEST_GROUP})

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_service.get_settings",
                return_value=ws_settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir",
                ws_settings.data_dir / "ipc",
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_service._get_action_catalog",
                return_value=catalog,
            ),
        ):
            data = {
                "type": "service:safe_tool",
                "request_id": "safe-req-1",
            }
            await registry.dispatch(data, "mygroup", False, deps)

        # Handler called immediately
        mock_handler.assert_awaited_once()

        # Response written immediately (no approval needed)
        response_path = tmp_path / "ipc" / "mygroup" / "responses" / "safe-req-1.json"
        assert response_path.exists()

        # No pending approval created
        pending_dir = tmp_path / "approvals" / "mygroup" / "pending_approvals"
        assert not pending_dir.exists() or not list(pending_dir.glob("*.json"))

        # No notification broadcast
        assert len(deps.broadcast_messages) == 0
