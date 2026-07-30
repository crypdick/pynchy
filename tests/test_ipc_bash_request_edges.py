"""Bash security IPC request-envelope contracts."""

from __future__ import annotations

from pynchy.host.container_manager.ipc.registry import dispatch
from pynchy.workspace.api import WorkspaceProfile
from tests.ipc_bash_security_support import _Deps


async def test_bash_security_check_without_request_id_is_ignored() -> None:
    workspace = WorkspaceProfile(
        jid="discord:channel:1",
        name="Test",
        folder="test-ws",
        trigger="always",
    )

    await dispatch(
        {"type": "security:bash_check", "command": "curl https://example.test"},
        "test-ws",
        False,
        _Deps(workspace),
    )
