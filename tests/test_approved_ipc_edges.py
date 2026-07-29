"""Approved IPC dispatch boundary contracts."""

from __future__ import annotations

import json

from pynchy.host.container_manager.ipc.write import configure_ipc_base_dir
from pynchy.host.container_manager.security.approved_ipc import execute_approved_ipc


async def test_approved_ipc_without_dependencies_writes_internal_error(tmp_path) -> None:
    configure_ipc_base_dir(tmp_path)

    await execute_approved_ipc({}, "project", "request-1", "operation", None)

    response = tmp_path / "project" / "responses" / "request-1.json"
    assert json.loads(response.read_text()) == {
        "error": "Internal error: IPC approval missing deps"
    }
