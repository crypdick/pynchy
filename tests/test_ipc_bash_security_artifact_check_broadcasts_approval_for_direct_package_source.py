"""Tests for the bash security check IPC handler."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from conftest import make_settings

import pynchy.host.container_manager.ipc.registry as registry
from pynchy import state
from pynchy.host.container_manager.security.gate import SecurityGate
from pynchy.plugins.api import OutboundEventType
from pynchy.workspace.api import (
    WorkspaceProfile,
    WorkspaceSecurity,
)
from tests.ipc_bash_security_support import (
    _Deps,
    _make_gate,
)


@pytest.mark.asyncio
async def test_artifact_check_broadcasts_approval_for_direct_package_source(tmp_path):
    """A human-gated package check must persist the approval against its chat."""
    await state.init_test_database()
    workspace = WorkspaceProfile(
        jid="discord:channel:1",
        name="Test",
        folder="test-ws",
        trigger="always",
    )
    deps = _Deps(workspace)
    settings = make_settings(data_dir=tmp_path)

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_artifact_security.get_gate_for_group",
            return_value=_make_gate(),
        ),
        patch(
            "pynchy.host.container_manager.security.approval._approval_root",
            settings.data_dir / "approvals",
        ),
    ):
        await registry.dispatch(
            {
                "type": "security:artifact_check",
                "request_id": "artifact-direct-source",
                "tool_name": "Bash",
                "packages": [
                    {
                        "ecosystem": "pypi",
                        "name": "example-package",
                        "version": "1.2.3",
                        "source": "direct_url",
                        "intent": "dependency",
                        "lock_pinned": False,
                    }
                ],
            },
            "test-ws",
            False,
            deps,
        )

    pending_path = (
        tmp_path / "approvals" / "test-ws" / "pending_approvals" / "artifact-direct-source.json"
    )
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    assert pending["approval_chat_jid"] == workspace.jid
    assert pending["handler_type"] == "security_artifact"
    assert len(deps.events) == 1
    assert deps.events[0].type is OutboundEventType.APPROVAL


def test_confirmed_credential_path_taints_when_workspace_profile_is_misconfigured():
    gate = SecurityGate(WorkspaceSecurity(contains_secrets=False))

    gate.confirm_credential_access()

    assert gate.secret_tainted is True
