"""Boundary coverage for file-backed approval decision handling."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from conftest import NullIpcDeps, init_test_database, make_host_action_catalog, make_settings

from pynchy.host.container_manager.ipc.approval_decision_context import ApprovalDecisionContext
from pynchy.host.container_manager.ipc.handlers_approval import process_approval_decision
from pynchy.host.container_manager.security.gate import SecurityGate
from pynchy.plugins.api import HostActionCatalog
from pynchy.workspace.api import CapabilityRule, WorkspaceSecurity
from tests.action_intents_support import (
    _MATRIX_FOLDER,
    _matrix_control_binding,
    _resolved_matrix_workspace,
    _write_matrix_approval,
)
from tests.approval_support import write_encrypted_pending_approval

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def _setup_db():
    await init_test_database()


@pytest.fixture
def ipc_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "ipc"
    directory.mkdir()
    return directory


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
    capability_id: str | None = None,
) -> Path:
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
        capability_id=capability_id,
    )
    return path


def _write_decision(
    ipc_dir: Path,
    group: str,
    request_id: str,
    *,
    approved: bool,
    approval_scope: str | None = None,
) -> Path:
    decisions_dir = ipc_dir.parent / "approvals" / group / "approval_decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    pending_path = ipc_dir.parent / "approvals" / group / "pending_approvals" / f"{request_id}.json"
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    decision = {
        "request_id": request_id,
        "guarded_action_id": pending["guarded_action_id"],
        "request_payload_hash": pending["request_payload_hash"],
        "source_group": group,
        "approved": approved,
        "decided_by": "testuser",
        "decided_at": "2026-02-24T12:01:00+00:00",
    }
    if approval_scope is not None:
        decision["approval_scope"] = approval_scope
    path = decisions_dir / f"{request_id}.json"
    path.write_text(json.dumps(decision), encoding="utf-8")
    return path


class TestApprovalBoundaryEdges:
    pytestmark = pytest.mark.usefixtures("_setup_db")

    @pytest.mark.asyncio
    async def test_decision_outside_host_approval_directory_is_rejected(
        self, ipc_dir: Path, settings
    ):
        pending_file = _write_pending(ipc_dir, "grp", "outside", "my_tool", {})
        host_decision = _write_decision(ipc_dir, "grp", "outside", approved=True)
        agent_decision = ipc_dir / "grp" / "approval_decisions" / host_decision.name
        agent_decision.parent.mkdir(parents=True)
        agent_decision.write_text(host_decision.read_text(encoding="utf-8"), encoding="utf-8")

        with patch(
            "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
            return_value=settings,
        ):
            await process_approval_decision(agent_decision, "grp")

        assert not agent_decision.exists()
        assert pending_file.exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("pending_contents", ["{", "[]"])
    async def test_invalid_pending_state_is_cleaned_with_decision(
        self, ipc_dir: Path, settings, pending_contents: str
    ):
        pending_file = _write_pending(ipc_dir, "grp", "invalid-pending", "my_tool", {})
        decision_file = _write_decision(ipc_dir, "grp", "invalid-pending", approved=True)
        pending_file.write_text(pending_contents, encoding="utf-8")

        with patch(
            "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
            return_value=settings,
        ):
            await process_approval_decision(decision_file, "grp")

        assert not pending_file.exists()
        assert not decision_file.exists()

    @pytest.mark.asyncio
    async def test_pending_identity_mismatch_rejects_decision(self, ipc_dir: Path, settings):
        pending_file = _write_pending(ipc_dir, "grp", "identity", "my_tool", {})
        decision_file = _write_decision(ipc_dir, "grp", "identity", approved=True)
        pending = json.loads(pending_file.read_text(encoding="utf-8"))
        pending["request_id"] = "different-request"
        pending_file.write_text(json.dumps(pending), encoding="utf-8")

        with patch(
            "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
            return_value=settings,
        ):
            await process_approval_decision(decision_file, "grp")

        assert pending_file.exists()
        assert not decision_file.exists()

    @pytest.mark.asyncio
    async def test_malformed_pending_fields_are_cleaned(self, ipc_dir: Path, settings):
        pending_file = _write_pending(ipc_dir, "grp", "malformed-pending", "my_tool", {})
        decision_file = _write_decision(ipc_dir, "grp", "malformed-pending", approved=True)
        pending = json.loads(pending_file.read_text(encoding="utf-8"))
        pending["tool_name"] = ""
        pending_file.write_text(json.dumps(pending), encoding="utf-8")

        with patch(
            "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
            return_value=settings,
        ):
            await process_approval_decision(decision_file, "grp")

        assert not pending_file.exists()
        assert not decision_file.exists()

    @pytest.mark.asyncio
    async def test_replay_uses_configured_security_source(self, ipc_dir: Path, settings):
        _write_pending(ipc_dir, "grp", "configured-security", "my_tool", {})
        decision_file = _write_decision(ipc_dir, "grp", "configured-security", approved=False)

        def replay_gate(_group, *, policy, **_kwargs):
            assert policy.configured_security("grp") is None
            return SecurityGate(WorkspaceSecurity())

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.approval_replay_gate",
                side_effect=replay_gate,
            ),
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"
            ),
        ):
            await process_approval_decision(decision_file, "grp", deps=NullIpcDeps())

        response = json.loads((ipc_dir / "grp/responses/configured-security.json").read_text())
        assert response == {"error": "Denied by user"}

    @pytest.mark.asyncio
    async def test_mcp_proxy_decision_resolves_or_logs_missing_future(
        self, ipc_dir: Path, settings
    ):
        _write_pending(ipc_dir, "grp", "proxy-decision", "proxy_tool", {}, handler_type="mcp_proxy")
        decision_file = _write_decision(ipc_dir, "grp", "proxy-decision", approved=True)

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.approval_replay_gate",
                return_value=SecurityGate(WorkspaceSecurity()),
            ),
            patch(
                "pynchy.host.container_manager.security.approval.resolve_mcp_proxy_approval",
                return_value=False,
            ) as resolve,
        ):
            await process_approval_decision(decision_file, "grp")

        resolve.assert_called_once_with("proxy-decision", approved=True)
        assert not decision_file.exists()

    @pytest.mark.asyncio
    async def test_mcp_proxy_decision_resolves_existing_future(self, ipc_dir: Path, settings):
        _write_pending(ipc_dir, "grp", "proxy-resolved", "proxy_tool", {}, handler_type="mcp_proxy")
        decision_file = _write_decision(ipc_dir, "grp", "proxy-resolved", approved=True)

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.approval_replay_gate",
                return_value=SecurityGate(WorkspaceSecurity()),
            ),
            patch(
                "pynchy.host.container_manager.security.approval.resolve_mcp_proxy_approval",
                return_value=True,
            ) as resolve,
        ):
            await process_approval_decision(decision_file, "grp")

        resolve.assert_called_once_with("proxy-resolved", approved=True)
        assert not decision_file.exists()

    @pytest.mark.asyncio
    async def test_mcp_proxy_session_decision_applies_reusable_grant(self, ipc_dir: Path, settings):
        _write_pending(
            ipc_dir,
            "grp",
            "proxy-session",
            "linear_get_issue",
            {},
            handler_type="mcp_proxy",
            capability_id="mcp.linear.linear_get_issue",
        )
        decision_file = _write_decision(
            ipc_dir,
            "grp",
            "proxy-session",
            approved=True,
            approval_scope="session",
        )
        reusable = AsyncMock(return_value=True)

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.approval_replay_gate",
                return_value=SecurityGate(WorkspaceSecurity()),
            ),
            patch(
                "pynchy.host.container_manager.ipc.approval_grants.approval_replay_validation_error",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "pynchy.host.container_manager.ipc.approval_grants.apply_reusable_approval",
                new=reusable,
            ),
            patch(
                "pynchy.host.container_manager.security.approval.resolve_mcp_proxy_approval",
                return_value=True,
            ) as resolve,
        ):
            await process_approval_decision(decision_file, "grp", deps=NullIpcDeps())

        reusable.assert_awaited_once()
        resolve.assert_called_once_with("proxy-session", approved=True)

    @pytest.mark.asyncio
    async def test_mcp_proxy_binding_rejection_closes_intent_and_writes_error(
        self, ipc_dir: Path, settings
    ):
        _write_pending(ipc_dir, "grp", "proxy-binding", "proxy_tool", {}, handler_type="mcp_proxy")
        decision_file = _write_decision(ipc_dir, "grp", "proxy-binding", approved=True)
        decision = json.loads(decision_file.read_text(encoding="utf-8"))
        decision["request_payload_hash"] = "changed"
        decision_file.write_text(json.dumps(decision), encoding="utf-8")

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.container_manager.security.approval.resolve_mcp_proxy_approval",
                return_value=False,
            ) as resolve,
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"
            ),
        ):
            await process_approval_decision(decision_file, "grp", deps=NullIpcDeps())

        resolve.assert_called_once_with("proxy-binding", approved=False)
        response = json.loads((ipc_dir / "grp/responses/proxy-binding.json").read_text())
        assert "Approval rejected" in response["error"]

    @pytest.mark.asyncio
    async def test_security_approval_writes_allow_response(self, ipc_dir: Path, settings):
        _write_pending(
            ipc_dir, "grp", "security-approval", "Bash", {}, handler_type="security_bash"
        )
        decision_file = _write_decision(ipc_dir, "grp", "security-approval", approved=True)

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.approval_replay_gate",
                return_value=SecurityGate(WorkspaceSecurity()),
            ),
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"
            ),
        ):
            await process_approval_decision(decision_file, "grp", deps=NullIpcDeps())

        response = json.loads((ipc_dir / "grp/responses/security-approval.json").read_text())
        assert response == {
            "result": {"decision": "allow", "guarded_action_id": "security-approval"}
        }

    @pytest.mark.asyncio
    async def test_concurrent_decision_processing_executes_approved_ipc_once(
        self, ipc_dir: Path, settings
    ):
        _write_pending(
            ipc_dir,
            "grp",
            "concurrent-approval",
            "test_operation",
            {},
            handler_type="ipc",
        )
        decision_file = _write_decision(ipc_dir, "grp", "concurrent-approval", approved=True)
        dispatch_started = asyncio.Event()
        duplicate_dispatch_started = asyncio.Event()
        release_dispatch = asyncio.Event()

        async def dispatch(*_args, **_kwargs):
            if dispatch_started.is_set():
                duplicate_dispatch_started.set()
            else:
                dispatch_started.set()
            await release_dispatch.wait()

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.approval_replay_gate",
                return_value=SecurityGate(WorkspaceSecurity()),
            ),
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir",
                settings.data_dir / "ipc",
            ),
            patch(
                "pynchy.host.container_manager.ipc.registry.dispatch",
                new=AsyncMock(side_effect=dispatch),
            ) as approved_dispatch,
        ):
            first = asyncio.create_task(
                process_approval_decision(decision_file, "grp", deps=NullIpcDeps())
            )
            await dispatch_started.wait()
            second = asyncio.create_task(
                process_approval_decision(decision_file, "grp", deps=NullIpcDeps())
            )
            try:
                await asyncio.wait_for(second, timeout=0.1)
                assert not duplicate_dispatch_started.is_set()
            finally:
                release_dispatch.set()
                await asyncio.gather(first, second, return_exceptions=True)

        approved_dispatch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_policy_disappearance_after_validation_fails_closed(
        self, ipc_dir: Path, settings
    ):
        _write_pending(ipc_dir, "grp", "policy-disappeared", "my_tool", {})
        decision_file = _write_decision(ipc_dir, "grp", "policy-disappeared", approved=True)
        action = make_host_action_catalog("my_tool", handler=AsyncMock()).actions[0]
        context = ApprovalDecisionContext(
            request_id="policy-disappeared",
            source_group="grp",
            tool_name="my_tool",
            chat_jid="j@g.us",
            request_data={},
            approved=True,
            approver="testuser",
            approved_at="2026-02-24T12:01:00+00:00",
            handler_type="service",
            action=action,
            gate=None,
            capability_id="test.my_tool",
            action_ids=(),
            origin_conversation_id=None,
            action_payload=None,
            action_payload_sha256=None,
            requested_at=None,
            expires_after_seconds=3600,
        )

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval._build_approval_decision_context",
                return_value=context,
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval._approval_replay_validation_error",
                new=AsyncMock(return_value=None),
            ),
            pytest.raises(RuntimeError, match="policy disappeared"),
        ):
            await process_approval_decision(decision_file, "grp", deps=NullIpcDeps())

    @pytest.mark.asyncio
    async def test_reusable_approval_failure_stops_before_dispatch(self, ipc_dir: Path, settings):
        _write_pending(ipc_dir, "grp", "reusable-failed", "my_tool", {})
        decision_file = _write_decision(ipc_dir, "grp", "reusable-failed", approved=True)

        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir",
                settings.data_dir / "ipc",
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.approval_replay_gate",
                return_value=SecurityGate(WorkspaceSecurity()),
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.apply_reusable_approval",
                new=AsyncMock(return_value=False),
            ) as reusable,
        ):
            await process_approval_decision(decision_file, "grp", deps=NullIpcDeps())

        reusable.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_approved_action_intent_is_denied_when_policy_changes(self, tmp_path: Path):
        request_id = "policy-denied-action"
        action, provider, _pending, decision_file = await _write_matrix_approval(
            tmp_path, request_id=request_id
        )
        provider.reset_mock()
        deps = NullIpcDeps()
        deps.get_conversation_control_binding = AsyncMock(return_value=_matrix_control_binding())
        settings = make_settings(data_dir=tmp_path)
        gate = SecurityGate(
            WorkspaceSecurity(
                capabilities={str(action.capability.id): CapabilityRule(decision="deny")}
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
                return_value=HostActionCatalog(actions=(action,)),
            ),
            patch(
                "pynchy.host.container_manager.ipc.handlers_approval.approval_replay_gate",
                return_value=gate,
            ),
            patch(
                "pynchy.config.settings.Settings.resolved_workspace_config",
                return_value=_resolved_matrix_workspace(),
            ),
        ):
            await process_approval_decision(decision_file, _MATRIX_FOLDER, deps=deps)

        provider.assert_not_awaited()
        response = json.loads(
            (tmp_path / "ipc" / _MATRIX_FOLDER / "responses" / decision_file.name).read_text()
        )
        assert "blocked by current policy" in response["error"]
