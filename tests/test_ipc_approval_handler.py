"""Tests for the IPC approval decision handler."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import NullIpcDeps, init_test_database, make_host_action_catalog, make_settings

from pynchy.host.container_manager.ipc.approval_decision_context import (
    ApprovalDecision,
    build_approval_decision_context,
)
from pynchy.host.container_manager.ipc.approval_replay import (
    ApprovalReplayPolicy,
    approval_replay_gate,
)
from pynchy.host.container_manager.ipc.handlers_approval import process_approval_decision
from pynchy.host.container_manager.security.gate import SecurityGate
from pynchy.plugins.api import ApprovalMode
from pynchy.workspace.api import (
    CapabilityRule,
    WorkspaceSecurity,
)
from tests.approval_support import write_encrypted_pending_approval

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def _setup_db():
    await init_test_database()


@pytest.fixture
def ipc_dir(tmp_path: Path) -> Path:
    d = tmp_path / "ipc"
    d.mkdir()
    return d


@pytest.fixture
def settings(tmp_path: Path):
    return make_settings(data_dir=tmp_path)


def _write_pending(
    ipc_dir: Path,
    group: str,
    request_id: str,
    tool_name: str,
    request_data: dict,
    handler_type: str = "service",
) -> Path:
    """Helper to write a pending approval file."""
    executable_request = {
        "type": f"service:{tool_name}" if handler_type == "service" else tool_name,
        "request_id": request_id,
        **request_data,
    }
    path, _pending = write_encrypted_pending_approval(
        ipc_dir.parent / "approvals",
        request_id=request_id,
        tool_name=tool_name,
        source_group=group,
        approval_chat_jid="j@g.us",
        request_data=executable_request,
        handler_type=handler_type,
        expires_after_seconds=3600,
    )
    return path


def _write_decision(ipc_dir: Path, group: str, request_id: str, *, approved: bool) -> Path:
    """Helper to write a decision file."""
    decisions_dir = ipc_dir.parent / "approvals" / group / "approval_decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    pending_path = ipc_dir.parent / "approvals" / group / "pending_approvals" / f"{request_id}.json"
    pending = (
        json.loads(pending_path.read_text(encoding="utf-8"))
        if pending_path.exists()
        else {
            "guarded_action_id": request_id,
            "request_payload_hash": "orphan",
        }
    )
    data = {
        "request_id": request_id,
        "guarded_action_id": pending["guarded_action_id"],
        "request_payload_hash": pending["request_payload_hash"],
        "source_group": group,
        "approved": approved,
        "decided_by": "testuser",
        "decided_at": "2026-02-24T12:01:00+00:00",
    }
    filepath = decisions_dir / f"{request_id}.json"
    filepath.write_text(json.dumps(data))
    return filepath


class TestProcessApprovalDecision:
    pytestmark = pytest.mark.usefixtures("_setup_db")

    @pytest.mark.asyncio
    async def test_approved_executes_and_writes_response(self, ipc_dir: Path, settings):
        _write_pending(ipc_dir, "grp", "req123", "my_tool", {"arg": "val"})
        decision_file = _write_decision(ipc_dir, "grp", "req123", approved=True)

        mock_handler = AsyncMock(return_value={"result": {"status": "posted"}})
        catalog = make_host_action_catalog(
            "my_tool",
            handler=mock_handler,
            approval_mode=ApprovalMode.SESSION_TOOL,
        )
        current_gate = SecurityGate(WorkspaceSecurity())

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"
            ),
            patch(
                "pynchy.host.container_manager.ipc.approval_decision_context._get_action_catalog",
                return_value=catalog,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.approval_replay_gate",
                return_value=current_gate,
            ),
        ):
            await process_approval_decision(decision_file, "grp", deps=NullIpcDeps())

        # Handler was called with original request data
        mock_handler.assert_awaited_once()
        call_data = mock_handler.call_args[0][0]
        assert call_data["arg"] == "val"

        # Response file written
        response_file = ipc_dir / "grp" / "responses" / "req123.json"
        assert response_file.exists()
        response = json.loads(response_file.read_text())
        assert response["result"]["status"] == "posted"
        assert current_gate.has_session_tool_approval("my_tool")

        # Pending and decision files cleaned up
        assert not (
            ipc_dir.parent / "approvals" / "grp" / "pending_approvals" / "req123.json"
        ).exists()
        assert not decision_file.exists()

    @pytest.mark.asyncio
    async def test_denied_writes_error_response(self, ipc_dir: Path, settings):
        _write_pending(ipc_dir, "grp", "req456", "my_tool", {})
        decision_file = _write_decision(ipc_dir, "grp", "req456", approved=False)

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"
            ),
        ):
            await process_approval_decision(decision_file, "grp", deps=NullIpcDeps())

        response_file = ipc_dir / "grp" / "responses" / "req456.json"
        response = json.loads(response_file.read_text())
        assert "error" in response
        assert "denied" in response["error"].lower()

        # Cleaned up
        assert not (
            ipc_dir.parent / "approvals" / "grp" / "pending_approvals" / "req456.json"
        ).exists()
        assert not decision_file.exists()

    @pytest.mark.asyncio
    async def test_missing_pending_cleans_decision(self, ipc_dir: Path, settings):
        """Decision with no matching pending file should be cleaned up."""
        decision_file = _write_decision(ipc_dir, "grp", "orphan", approved=True)

        with patch(
            "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
            return_value=settings,
        ):
            await process_approval_decision(decision_file, "grp", deps=NullIpcDeps())

        assert not decision_file.exists()

    @pytest.mark.asyncio
    async def test_agent_mounted_decision_cannot_approve_host_pending(
        self, ipc_dir: Path, settings
    ):
        """A forged decision in the writable IPC mount is never authoritative."""
        pending_file = _write_pending(ipc_dir, "grp", "forged", "my_tool", {})
        forged_dir = ipc_dir / "grp" / "approval_decisions"
        forged_dir.mkdir(parents=True)
        forged_decision = forged_dir / "forged.json"
        forged_decision.write_text(
            json.dumps(
                {
                    "request_id": "forged",
                    "approved": True,
                    "decided_by": "agent",
                    "decided_at": datetime.now(UTC).isoformat(),
                }
            )
        )
        handler = AsyncMock(return_value={"result": "forged"})

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.approval_decision_context._get_action_catalog",
                return_value=make_host_action_catalog("my_tool", handler=handler),
            ),
        ):
            await process_approval_decision(forged_decision, "grp")

        handler.assert_not_awaited()
        assert pending_file.exists()
        assert not forged_decision.exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "invalid_field",
        [
            {"approved": "true"},
            {"decided_by": ""},
            {"decided_at": "2026-07-19T12:00:00"},
            {"unexpected": "field"},
        ],
    )
    async def test_malformed_decision_is_rejected(
        self,
        ipc_dir: Path,
        settings,
        invalid_field: dict[str, object],
    ):
        pending_file = _write_pending(ipc_dir, "grp", "invalid", "my_tool", {})
        decision_file = _write_decision(ipc_dir, "grp", "invalid", approved=True)
        decision = json.loads(decision_file.read_text())
        decision.update(invalid_field)
        decision_file.write_text(json.dumps(decision))
        handler = AsyncMock(return_value={"result": "invalid"})

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.approval_decision_context._get_action_catalog",
                return_value=make_host_action_catalog("my_tool", handler=handler),
            ),
        ):
            await process_approval_decision(decision_file, "grp", deps=NullIpcDeps())

        handler.assert_not_awaited()
        assert pending_file.exists()
        assert not decision_file.exists()

    @pytest.mark.asyncio
    async def test_request_time_taint_survives_gate_loss(self, ipc_dir: Path, settings):
        """Durable taint is reapplied even when the original in-memory gate is gone."""
        pending_file = _write_pending(ipc_dir, "grp", "tainted", "my_tool", {})
        pending = json.loads(pending_file.read_text())
        pending["corruption_tainted"] = True
        pending["redaction_required"] = "required"
        pending_file.write_text(json.dumps(pending))
        decision_file = _write_decision(ipc_dir, "grp", "tainted", approved=True)
        replay_gate = MagicMock(return_value=SecurityGate(WorkspaceSecurity()))
        handler = AsyncMock(return_value={"result": "ok"})

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"
            ),
            patch(
                "pynchy.host.container_manager.ipc.approval_decision_context._get_action_catalog",
                return_value=make_host_action_catalog("my_tool", handler=handler),
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.approval_replay_gate",
                replay_gate,
            ),
        ):
            await process_approval_decision(decision_file, "grp")

        replay_gate.assert_called_once()
        args, kwargs = replay_gate.call_args
        assert args == ("grp",)
        policy = kwargs.pop("policy")
        assert isinstance(policy, ApprovalReplayPolicy)
        assert callable(policy.configured_security)
        assert kwargs == {
            "require_resolved": False,
            "request_corruption_tainted": True,
            "request_secret_tainted": True,
        }

    def test_unresolved_workspace_replay_gate_reapplies_persisted_taints(self, settings):
        """Fallback policy retains durable taint after the active gate is lost."""
        with (
            patch(
                "pynchy.host.container_manager.ipc.approval_replay.get_gate_for_group",
                return_value=None,
            ),
            patch(
                "pynchy.host.container_manager.ipc.approval_replay.resolve_security",
                return_value=WorkspaceSecurity(),
            ),
        ):
            gate = approval_replay_gate(
                "unconfigured-workspace",
                policy=ApprovalReplayPolicy(
                    configured_security=lambda _group: WorkspaceSecurity(),
                    workspace_tools=lambda _group: None,
                ),
                request_corruption_tainted=True,
                request_secret_tainted=True,
            )

        assert gate is not None
        assert gate.corruption_tainted is True
        assert gate.secret_tainted is True

    @pytest.mark.parametrize(
        "taint_evidence",
        [
            {},
            {"corruption_tainted": "false", "secret_tainted": None},
        ],
    )
    def test_missing_or_malformed_persisted_taint_fails_closed(
        self,
        settings,
        taint_evidence: dict[str, object],
    ):
        pending = {
            "tool_name": "my_tool",
            "approval_chat_jid": "j@g.us",
            "request_data": {"type": "service:my_tool", "request_id": "taint-evidence"},
            "handler_type": "service",
            **taint_evidence,
        }
        decision = ApprovalDecision(
            request_id="taint-evidence",
            approved=True,
            decided_by="operator",
            decided_at="2026-07-19T12:00:00+00:00",
        )
        with (
            patch(
                "pynchy.host.container_manager.ipc.approval_decision_context._get_action_catalog",
                return_value=make_host_action_catalog("my_tool", handler=AsyncMock()),
            ),
            patch(
                "pynchy.host.container_manager.ipc.approval_replay.get_gate_for_group",
                return_value=None,
            ),
            patch(
                "pynchy.host.container_manager.ipc.approval_replay.resolve_security",
                return_value=WorkspaceSecurity(),
            ),
        ):
            context = build_approval_decision_context(
                pending,
                decision,
                source_group="unconfigured-workspace",
                replay_gate=lambda **kwargs: approval_replay_gate(
                    "unconfigured-workspace",
                    policy=ApprovalReplayPolicy(
                        configured_security=lambda _group: WorkspaceSecurity(),
                        workspace_tools=lambda _group: None,
                    ),
                    **kwargs,
                ),
            )

        assert context.gate is not None
        assert context.gate.corruption_tainted is True
        assert context.gate.secret_tainted is True

    @pytest.mark.asyncio
    async def test_unknown_tool_writes_error(self, ipc_dir: Path, settings):
        """Approved request for unknown tool should write error response."""
        _write_pending(ipc_dir, "grp", "req789", "nonexistent_tool", {})
        decision_file = _write_decision(ipc_dir, "grp", "req789", approved=True)
        record_event = AsyncMock()

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"
            ),
            patch(
                "pynchy.host.container_manager.ipc.approval_decision_context._get_action_catalog",
                return_value=make_host_action_catalog(handler=AsyncMock()),
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.record_security_event",
                record_event,
            ),
        ):
            await process_approval_decision(decision_file, "grp", deps=NullIpcDeps())

        response_file = ipc_dir / "grp" / "responses" / "req789.json"
        response = json.loads(response_file.read_text())
        assert "error" in response
        assert record_event.await_count == 2
        record_event.assert_any_await(
            chat_jid="j@g.us",
            workspace="grp",
            tool_name="nonexistent_tool",
            decision="execution_failed",
            reason="Host action descriptor is unavailable",
            request_id="req789",
        )

    @pytest.mark.asyncio
    async def test_handler_exception_writes_error(self, ipc_dir: Path, settings):
        """If the handler raises, write an error response instead of crashing."""
        _write_pending(ipc_dir, "grp", "reqfail", "bad_tool", {})
        decision_file = _write_decision(ipc_dir, "grp", "reqfail", approved=True)

        mock_handler = AsyncMock(side_effect=RuntimeError("boom"))
        catalog = make_host_action_catalog("bad_tool", handler=mock_handler)

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"
            ),
            patch(
                "pynchy.host.container_manager.ipc.approval_decision_context._get_action_catalog",
                return_value=catalog,
            ),
        ):
            await process_approval_decision(decision_file, "grp", deps=NullIpcDeps())

        response_file = ipc_dir / "grp" / "responses" / "reqfail.json"
        response = json.loads(response_file.read_text())
        assert "error" in response
        assert "boom" in response["error"]

    @pytest.mark.asyncio
    async def test_approved_replay_rechecks_current_policy(self, ipc_dir: Path, settings):
        """Human approval cannot override a policy denial added while waiting."""
        _write_pending(ipc_dir, "grp", "policy-changed", "my_tool", {})
        decision_file = _write_decision(
            ipc_dir,
            "grp",
            "policy-changed",
            approved=True,
        )
        mock_handler = AsyncMock(return_value={"result": "unsafe"})
        catalog = make_host_action_catalog("my_tool", handler=mock_handler)
        current_gate = SecurityGate(
            WorkspaceSecurity(
                capabilities={
                    "test.my.tool": CapabilityRule(decision="deny"),
                }
            )
        )

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"
            ),
            patch(
                "pynchy.host.container_manager.ipc.approval_decision_context._get_action_catalog",
                return_value=catalog,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.approval_replay_gate",
                return_value=current_gate,
            ),
        ):
            await process_approval_decision(decision_file, "grp", deps=NullIpcDeps())

        mock_handler.assert_not_awaited()
        response = json.loads((ipc_dir / "grp/responses/policy-changed.json").read_text())
        assert "blocked by current policy" in response["error"]

    @pytest.mark.asyncio
    async def test_changed_payload_is_rejected_before_dispatch(self, ipc_dir: Path, settings):
        """The approved payload cannot be changed while awaiting replay."""
        pending_file = _write_pending(ipc_dir, "grp", "changed", "my_tool", {"arg": "safe"})
        decision_file = _write_decision(ipc_dir, "grp", "changed", approved=True)
        reviewed_hash = json.loads(pending_file.read_text(encoding="utf-8"))["request_payload_hash"]
        _write_pending(
            ipc_dir,
            "grp",
            "changed",
            "my_tool",
            {"arg": "changed-after-review"},
        )
        changed = json.loads(pending_file.read_text(encoding="utf-8"))
        changed["request_payload_hash"] = reviewed_hash
        pending_file.write_text(json.dumps(changed), encoding="utf-8")
        handler = AsyncMock()

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"
            ),
            patch(
                "pynchy.host.container_manager.ipc.approval_decision_context._get_action_catalog",
                return_value=make_host_action_catalog("my_tool", handler=handler),
            ),
        ):
            await process_approval_decision(decision_file, "grp")

        handler.assert_not_awaited()
        response = json.loads((ipc_dir / "grp/responses/changed.json").read_text())
        assert "payload changed" in response["error"]

    @pytest.mark.asyncio
    async def test_cross_workspace_decision_is_rejected(self, ipc_dir: Path, settings):
        """A copied approval decision cannot authorize another workspace."""
        _write_pending(ipc_dir, "grp", "cross-workspace", "my_tool", {})
        decision_file = _write_decision(ipc_dir, "grp", "cross-workspace", approved=True)
        decision = json.loads(decision_file.read_text(encoding="utf-8"))
        decision["source_group"] = "another-workspace"
        decision_file.write_text(json.dumps(decision), encoding="utf-8")
        handler = AsyncMock()

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"
            ),
            patch(
                "pynchy.host.container_manager.ipc.approval_decision_context._get_action_catalog",
                return_value=make_host_action_catalog("my_tool", handler=handler),
            ),
        ):
            await process_approval_decision(decision_file, "grp")

        handler.assert_not_awaited()
        response = json.loads((ipc_dir / "grp/responses/cross-workspace.json").read_text())
        assert "another workspace" in response["error"]


class TestIpcApprovalDispatch:
    """Tests for handler_type="ipc" approval dispatch through the registry."""

    pytestmark = pytest.mark.usefixtures("_setup_db")

    @pytest.mark.asyncio
    async def test_ipc_approved_dispatches_through_registry(self, ipc_dir: Path, settings):
        """Approved IPC request dispatches through ipc._registry.dispatch()."""
        _write_pending(
            ipc_dir,
            "grp",
            "ipc-req1",
            "sync_worktree_to_main",
            {"diff": "fix typo"},
            handler_type="ipc",
        )
        decision_file = _write_decision(ipc_dir, "grp", "ipc-req1", approved=True)

        mock_deps = NullIpcDeps()
        mock_dispatch = AsyncMock()

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"
            ),
            patch("pynchy.host.container_manager.ipc.registry.dispatch", mock_dispatch),
        ):
            await process_approval_decision(decision_file, "grp", deps=mock_deps)

        mock_dispatch.assert_awaited_once()
        call_args = mock_dispatch.call_args
        dispatched_data = call_args.args[0]
        assert "_cop_approved" not in dispatched_data
        assert isinstance(dispatched_data["_approval_receipt"], str)
        assert call_args.args[1] == "grp"  # source_group
        assert call_args.kwargs["is_admin"] is True
        assert call_args.kwargs["deps"] is mock_deps

        # Cleaned up
        assert not (
            ipc_dir.parent / "approvals" / "grp" / "pending_approvals" / "ipc-req1.json"
        ).exists()
        assert not decision_file.exists()

    @pytest.mark.asyncio
    async def test_ipc_approved_without_deps_writes_error(self, ipc_dir: Path, settings):
        """IPC approval without deps writes an error response."""
        _write_pending(
            ipc_dir,
            "grp",
            "ipc-req2",
            "sync_worktree_to_main",
            {},
            handler_type="ipc",
        )
        decision_file = _write_decision(ipc_dir, "grp", "ipc-req2", approved=True)

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"
            ),
        ):
            await process_approval_decision(decision_file, "grp")  # No deps!

        response_file = ipc_dir / "grp" / "responses" / "ipc-req2.json"
        response = json.loads(response_file.read_text())
        assert "error" in response
        assert "dependencies" in response["error"].lower()

    @pytest.mark.asyncio
    async def test_ipc_dispatch_failure_writes_error(self, ipc_dir: Path, settings):
        """If IPC dispatch raises, write an error response."""
        _write_pending(
            ipc_dir,
            "grp",
            "ipc-req3",
            "sync_worktree_to_main",
            {},
            handler_type="ipc",
        )
        decision_file = _write_decision(ipc_dir, "grp", "ipc-req3", approved=True)

        mock_deps = NullIpcDeps()
        mock_dispatch = AsyncMock(side_effect=RuntimeError("dispatch failed"))

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"
            ),
            patch("pynchy.host.container_manager.ipc.registry.dispatch", mock_dispatch),
        ):
            await process_approval_decision(decision_file, "grp", deps=mock_deps)

        response_file = ipc_dir / "grp" / "responses" / "ipc-req3.json"
        response = json.loads(response_file.read_text())
        assert "error" in response
        assert "dispatch failed" in response["error"]
