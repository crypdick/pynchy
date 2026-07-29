"""Behavioral coverage for workspace container timeout selection."""

from __future__ import annotations

from pynchy.host.orchestrator.api import resolve_container_timeout
from pynchy.workspace.api import ContainerConfig, WorkspaceProfile


def test_workspace_container_timeout_overrides_host_default() -> None:
    workspace = WorkspaceProfile(
        jid="discord:channel:1",
        name="Slow integration",
        folder="integration",
        trigger="@Pynchy",
        container_config=ContainerConfig(timeout=90.0),
    )

    assert (
        resolve_container_timeout(workspace, default_timeout=30.0)
        == workspace.container_config.timeout
    )
