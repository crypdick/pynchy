"""Public agent-spawn behavior at the container orchestration boundary."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

from pynchy.host.container_manager.api import RepoMountResolution
from pynchy.host.container_manager.mcp.startup import McpWorkspaceStartup
from pynchy.host.container_manager.orchestrator import configure_container_spawn_runtime
from pynchy.workspace.api import RuntimeTarget
from tests.app_integration_support import (
    FakeProcess,
    _completed_awaitable,
    _patch_test_settings,
)

pytest_plugins = ("tests.app_integration_support",)

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from pathlib import Path

    from pynchy.host.orchestrator.app import PynchyApp


_CR_ORCH = "pynchy.host.container_manager.orchestrator"


async def test_agent_forwards_repo_resolution_notices_without_mcp_routes(
    app: PynchyApp, tmp_path: Path
):
    fake_proc = FakeProcess(
        output={
            "status": "success",
            "result": "repo notice handled",
            "new_session_id": "s-repo-notice",
        }
    )
    driver = asyncio.create_task(fake_proc.schedule_output())

    def fake_create(*args: Any, **kwargs: Any) -> Awaitable[FakeProcess]:
        return _completed_awaitable(fake_proc)

    configure_container_spawn_runtime(
        container_cli="docker",
        ensure_agent_image=lambda **_kwargs: None,
        resolve_repo_mounts=lambda _folder, _repos: RepoMountResolution(
            notices=("repository recovered with local changes",)
        ),
    )
    group = app.workspaces["group@g.us"]

    with (
        patch(f"{_CR_ORCH}.asyncio.create_subprocess_exec", fake_create),
        _patch_test_settings(tmp_path),
        patch(
            "pynchy.host.container_manager.mcp.manager.get_mcp_manager",
            return_value=MagicMock(
                get_workspace_instance_ids=MagicMock(return_value=[]),
                ensure_workspace_running=AsyncMock(return_value=McpWorkspaceStartup((), ())),
                get_direct_server_configs=MagicMock(return_value=[]),
            ),
        ),
    ):
        (tmp_path / "groups" / "test-group").mkdir(parents=True)
        result = await app.queue.run_serialized_task(
            RuntimeTarget.from_workspace(group),
            "test-run-repo-notice",
            lambda: app.run_agent(
                group,
                "group@g.us",
                [{"message_type": "user", "content": "check repository"}],
                repo_access_override="owner/pynchy",
            ),
        )

    await driver
    assert result == "success"
    initial_input = json.loads(
        (tmp_path / "data" / "ipc" / "test-group" / "input" / "initial.json").read_text()
    )
    assert initial_input["system_notices"] == ["repository recovered with local changes"]
