"""Tests for workspace reconciliation logic.

Tests reconcile_workspaces() which reads workspace configs from config.toml and
ensures scheduled tasks and chat groups are created. This is critical startup
logic — bugs here mean periodic agents silently don't run or get double-scheduled.
"""

from __future__ import annotations

from collections.abc import Callable  # noqa: TC003 - dataclass field type.
from dataclasses import dataclass

import pluggy

from pynchy.config.api import JobConfig, ProfileConfig, WorkspaceConfig


@dataclass
class _WorkspaceSpecHooks:
    """The pluggy hook subset used to collect plugin workspace specifications."""

    pynchy_workspace_spec: Callable[[], list[object]]


class _FakePM(pluggy.PluginManager):
    """Real-class stand-in so isinstance(pm, pluggy.PluginManager) succeeds."""

    def __init__(self, hook: _WorkspaceSpecHooks) -> None:
        self.hook = hook


class _WorkspaceHarness(dict[str, WorkspaceConfig]):
    def __init__(self) -> None:
        super().__init__()
        self.profiles: dict[str, ProfileConfig] = {}
        self.jobs: dict[str, JobConfig] = {}


def _write_workspace_yaml(workspaces, folder_name, data):
    """Compat helper: populate Settings.workspaces for tests."""
    d = data or {}
    profile_name = f"{folder_name}-profile"
    profile = ProfileConfig(
        is_admin=bool(d.get("is_admin", False)),
        repo=d.get("repo_access", []),
    )
    if isinstance(workspaces, _WorkspaceHarness):
        workspaces.profiles[profile_name] = profile
        if "schedule" in d and "prompt" in d:
            workspaces.jobs[folder_name] = JobConfig(
                workspace=folder_name,
                schedule=d["schedule"],
                prompt=d["prompt"],
            )
    workspace_data: dict[str, object] = {"profiles": [profile_name]}
    chat = d.get("chat")
    if isinstance(chat, str) and chat.startswith("connection."):
        workspace_data["chat"] = chat
    workspaces[folder_name] = WorkspaceConfig.model_validate(workspace_data)
