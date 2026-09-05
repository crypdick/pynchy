"""Tests for the container runner."""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - test fixtures mock subprocess behavior and exceptions
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pluggy

from pynchy.host.git_ops.api import RepoContext, get_repo_token
from pynchy.host.orchestrator.api import resolve_agent_core
from pynchy.ipc_snapshots import write_groups_snapshot, write_tasks_snapshot
from pynchy.plugins.api import AgentCoreSpec
from pynchy.workspace.api import (
    AdditionalMount,
    ContainerConfig,
    WorkspaceProfile,
)
from tests.container_runner_support import (
    _patch_settings,
    build_volume_mounts,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_GROUP = WorkspaceProfile(
    jid="test@g.us",
    name="Test Group",
    folder="test-group",
    trigger="@pynchy",
    added_at="2024-01-01T00:00:00.000Z",
)

_CR_CREDS = "pynchy.host.container_manager.credentials"
_CR_ORCH = "pynchy.host.container_manager.orchestrator"
_GATEWAY = "pynchy.host.container_manager.gateway"


_SETTINGS_MODULES = [
    "pynchy.host.orchestrator.workspace_config",
]

_test_settings: ContextVar[Any | None] = ContextVar("test_settings", default=None)


class TestMountBuilding:
    def test_additional_mounts_are_appended_after_validation(self, tmp_path: Path):
        group = WorkspaceProfile(
            jid="mounts@g.us",
            name="Mounts",
            folder="mounts",
            trigger="@pynchy",
            added_at="2024-01-01",
            container_config=ContainerConfig(
                additional_mounts=[AdditionalMount(host_path="/host/data")]
            ),
        )
        with (
            _patch_settings(tmp_path),
            patch(
                "pynchy.host.container_manager.mounts.validate_additional_mounts",
                return_value=[
                    {
                        "hostPath": "/host/data",
                        "containerPath": "/home/agent/mnt/data",
                        "readonly": True,
                    }
                ],
            ) as validate,
        ):
            mounts = build_volume_mounts(group, is_admin=False)

        validate.assert_called_once()
        assert mounts[-1].host_path == "/host/data"
        assert mounts[-1].container_path == "/home/agent/mnt/data"
        assert mounts[-1].readonly is True

    def test_repo_mounts_support_multiple_repos(self, tmp_path: Path):
        repo_a = RepoContext(
            slug="owner/pynchy", root=tmp_path / "repo-a", worktrees_dir=tmp_path / "wt-a"
        )
        repo_b = RepoContext(
            slug="owner/tools", root=tmp_path / "repo-b", worktrees_dir=tmp_path / "wt-b"
        )
        wt_a = tmp_path / "worktrees" / "pynchy"
        wt_b = tmp_path / "worktrees" / "tools"
        for path in (repo_a.root / ".git", repo_b.root / ".git", wt_a, wt_b):
            path.mkdir(parents=True)

        with _patch_settings(tmp_path):
            (tmp_path / "groups" / "multi").mkdir(parents=True)
            group = WorkspaceProfile(
                jid="multi@g.us",
                name="Multi",
                folder="multi",
                trigger="@pynchy",
                added_at="2024-01-01",
            )
            mounts = build_volume_mounts(
                group,
                is_admin=False,
                repo_mounts=[(repo_a, wt_a), (repo_b, wt_b)],
            )

        by_container = {m.container_path: m.host_path for m in mounts}
        assert by_container["/home/agent/src/owner/pynchy"] == str(wt_a)
        assert by_container["/home/agent/src/owner/tools"] == str(wt_b)

    def test_nonadmin_does_not_get_raw_host_repo_mount(self, tmp_path: Path):
        """Non-admin groups never get the raw host repo mount."""
        worktree_path = tmp_path / "worktrees" / "other"
        worktree_path.mkdir(parents=True)
        repo_ctx = RepoContext(
            slug="owner/pynchy", root=tmp_path, worktrees_dir=tmp_path / "worktrees"
        )
        with _patch_settings(tmp_path):
            (tmp_path / "groups" / "other").mkdir(parents=True)
            group = WorkspaceProfile(
                jid="other@g.us",
                name="Other",
                folder="other",
                trigger="@pynchy",
                added_at="2024-01-01",
            )
            mounts = build_volume_mounts(
                group, is_admin=False, repo_ctx=repo_ctx, worktree_path=worktree_path
            )

            paths = [m.container_path for m in mounts]
            assert "/danger/raw-host-repos/owner/pynchy" not in paths

    def test_admin_no_config_toml_when_missing(self, tmp_path: Path):
        """Admin group doesn't get config.toml mount if the file doesn't exist."""
        with _patch_settings(tmp_path):
            (tmp_path / "groups" / "admin-1").mkdir(parents=True)
            group = WorkspaceProfile(
                jid="admin-1@g.us",
                name="Admin",
                folder="admin-1",
                trigger="always",
                added_at="2024-01-01",
            )
            mounts = build_volume_mounts(group, is_admin=True)

            paths = [m.container_path for m in mounts]
            assert "/home/agent/src/owner/pynchy/config.toml" not in paths


class TestReadGhToken:
    """gh-CLI token discovery, driven through the public get_repo_token().

    With no per-repo token and no configured gh_token secret, get_repo_token()
    falls through to the gh CLI, so these exercise that discovery path.
    """

    def test_returns_token_from_gh_cli(self):
        mock_result = type("Result", (), {"returncode": 0, "stdout": "gho_test123\n"})()
        with patch(f"{_CR_CREDS}.subprocess.run", return_value=mock_result):
            assert get_repo_token("owner/repo") == "gho_test123"

    def test_returns_none_on_failure(self):
        mock_result = type("Result", (), {"returncode": 1, "stdout": ""})()
        with patch(f"{_CR_CREDS}.subprocess.run", return_value=mock_result):
            assert get_repo_token("owner/repo") is None

    def test_returns_none_when_gh_not_installed(self):
        with patch(f"{_CR_CREDS}.subprocess.run", side_effect=FileNotFoundError):
            assert get_repo_token("owner/repo") is None

    def test_returns_none_on_timeout(self):
        with patch(
            f"{_CR_CREDS}.subprocess.run",
            side_effect=subprocess.TimeoutExpired("gh", 5),
        ):
            assert get_repo_token("owner/repo") is None


class TestTasksSnapshot:
    def test_admin_sees_all_tasks(self, tmp_path: Path):
        with _patch_settings(tmp_path):
            tasks = [
                {"groupFolder": "admin-1", "id": "t1"},
                {"groupFolder": "other", "id": "t2"},
            ]
            write_tasks_snapshot(tmp_path / "data", "admin-1", tasks, is_admin=True)
            result = json.loads(
                (tmp_path / "data" / "ipc" / "admin-1" / "current_tasks.json").read_text()
            )
            assert len(result) == 2

    def test_nonadmin_sees_only_own_tasks(self, tmp_path: Path):
        with _patch_settings(tmp_path):
            tasks = [
                {"groupFolder": "admin-1", "id": "t1"},
                {"groupFolder": "other", "id": "t2"},
            ]
            write_tasks_snapshot(tmp_path / "data", "other", tasks, is_admin=False)
            result = json.loads(
                (tmp_path / "data" / "ipc" / "other" / "current_tasks.json").read_text()
            )
            assert len(result) == 1
            assert result[0]["id"] == "t2"

    def test_admin_includes_host_jobs(self, tmp_path: Path):
        with _patch_settings(tmp_path):
            tasks = [{"groupFolder": "admin-1", "id": "t1"}]
            host_jobs = [{"type": "host", "id": "h1", "name": "daily-backup"}]
            write_tasks_snapshot(
                tmp_path / "data", "admin-1", tasks, is_admin=True, host_jobs=host_jobs
            )
            result = json.loads(
                (tmp_path / "data" / "ipc" / "admin-1" / "current_tasks.json").read_text()
            )
            assert len(result) == 2
            assert result[0]["id"] == "t1"
            assert result[1]["id"] == "h1"
            assert result[1]["type"] == "host"

    def test_nonadmin_ignores_host_jobs(self, tmp_path: Path):
        with _patch_settings(tmp_path):
            tasks = [{"groupFolder": "other", "id": "t1"}]
            host_jobs = [{"type": "host", "id": "h1", "name": "daily-backup"}]
            write_tasks_snapshot(
                tmp_path / "data", "other", tasks, is_admin=False, host_jobs=host_jobs
            )
            result = json.loads(
                (tmp_path / "data" / "ipc" / "other" / "current_tasks.json").read_text()
            )
            assert len(result) == 1
            assert result[0]["id"] == "t1"


class TestGroupsSnapshot:
    def test_admin_sees_all_groups(self, tmp_path: Path):
        with _patch_settings(tmp_path):
            groups = [{"jid": "a@g.us"}, {"jid": "b@g.us"}]
            write_groups_snapshot(
                tmp_path / "data", "admin-1", groups, {"a@g.us", "b@g.us"}, is_admin=True
            )
            result = json.loads(
                (tmp_path / "data" / "ipc" / "admin-1" / "available_groups.json").read_text()
            )
            assert len(result["groups"]) == 2

    def test_nonadmin_sees_no_groups(self, tmp_path: Path):
        with _patch_settings(tmp_path):
            groups = [{"jid": "a@g.us"}]
            write_groups_snapshot(tmp_path / "data", "other", groups, {"a@g.us"}, is_admin=False)
            result = json.loads(
                (tmp_path / "data" / "ipc" / "other" / "available_groups.json").read_text()
            )
            assert len(result["groups"]) == 0


class TestResolveAgentCore:
    """Test agent core resolution from plugin manager.

    This selects which AI agent core (module + class) to use for container
    execution. Getting this wrong silently breaks all agent runs.
    """

    def test_returns_defaults_when_no_plugin_manager(self):
        """Covers the `if plugin_manager:` guard for the None case."""
        module, cls = resolve_agent_core(None, "openai")
        assert module == "agent_runner.cores.openai"
        assert cls == "OpenAIAgentCore"

    def test_returns_defaults_when_no_cores_registered(self):
        """Plugin manager exists but no agent core plugins are installed."""

        class FakeHook:
            def pynchy_agent_core_info(self):
                return []

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

        module, cls = resolve_agent_core(FakePM(), "openai")
        assert module == "agent_runner.cores.openai"
        assert cls == "OpenAIAgentCore"

    def test_uses_matching_core_by_name(self):
        """When a core matches DEFAULT_AGENT_CORE, use it."""

        class FakeHook:
            def pynchy_agent_core_info(self):
                return [
                    AgentCoreSpec(name="openai", module="cores.openai", class_name="OpenAICore"),
                    AgentCoreSpec(
                        name="claude", module="cores.claude_v2", class_name="ClaudeV2Core"
                    ),
                ]

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

        module, cls = resolve_agent_core(FakePM(), "claude")

        assert module == "cores.claude_v2"
        assert cls == "ClaudeV2Core"

    def test_falls_back_to_first_core_when_no_name_match(self):
        """If the configured DEFAULT_AGENT_CORE doesn't match any plugin, use the first one."""

        class FakeHook:
            def pynchy_agent_core_info(self):
                return [
                    AgentCoreSpec(name="openai", module="cores.openai", class_name="OpenAICore"),
                    AgentCoreSpec(name="gemini", module="cores.gemini", class_name="GeminiCore"),
                ]

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

        module, cls = resolve_agent_core(FakePM(), "claude")

        assert module == "cores.openai"
        assert cls == "OpenAICore"

    def test_exact_match_takes_priority_over_first(self):
        """When the desired core is second in the list, it still wins over first."""

        class FakeHook:
            def pynchy_agent_core_info(self):
                return [
                    AgentCoreSpec(name="openai", module="cores.openai", class_name="OpenAICore"),
                    AgentCoreSpec(name="custom", module="cores.custom", class_name="CustomCore"),
                ]

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

        module, cls = resolve_agent_core(FakePM(), "custom")

        assert module == "cores.custom"
        assert cls == "CustomCore"
