"""Tests for the container runner."""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pluggy
import pytest
from conftest import (
    make_host_runtime_operations,
    make_settings,
)

from pynchy.config.api import (
    LearningConfig,
    ObsidianLearningConfig,
    ProfileConfig,
    WorkspaceConfig,
    publish_settings,
)
from pynchy.host.container_manager.api import AgentHomeMounts
from pynchy.host.container_manager.mounts import MountOperations
from pynchy.host.git_ops.api import RepoContext
from pynchy.host.learning.paths import LearningConfigError
from pynchy.host.learning.skill_activation import (
    prepare_agent_homes,
    refresh_personalized_agent_skills,
)
from pynchy.host.orchestrator import host_execution
from pynchy.host.orchestrator.app import PynchyApp
from pynchy.workspace.api import (
    WorkspaceProfile,
)
from tests.container_runner_support import (
    _patch_settings,
    _profile_workspace,
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
    def test_agent_environment_is_not_written_or_mounted(self, tmp_path: Path):
        with _patch_settings(tmp_path, learning=LearningConfig(enabled=False)):
            mounts = build_volume_mounts(TEST_GROUP, is_admin=False)

        assert all(m.container_path != "/etc/pynchy/env" for m in mounts)
        assert not (tmp_path / "data/env").exists()

    def test_ipc_mount_prepares_host_owned_response_directory(self, tmp_path: Path):
        """The host owns request-response IPC directories before containers start."""
        with _patch_settings(tmp_path):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)

            build_volume_mounts(TEST_GROUP, is_admin=False)

        ipc_dir = tmp_path / "data" / "ipc" / "test-group"
        assert (ipc_dir / "requests").is_dir()
        assert (ipc_dir / "responses").is_dir()

    def test_learning_disabled_does_not_add_vault_mount(self, tmp_path: Path):
        with _patch_settings(tmp_path, learning=LearningConfig(enabled=False)):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)

            mounts = build_volume_mounts(TEST_GROUP, is_admin=False)

        assert all(m.container_path != "/home/agent/memory" for m in mounts)

    def test_all_agents_mount_personalization_skills_readwrite(self, tmp_path: Path):
        with _patch_settings(tmp_path, learning=LearningConfig(enabled=False)):
            mounts = build_volume_mounts(TEST_GROUP, is_admin=False)

        skill_mount = next(
            mount for mount in mounts if mount.container_path == "/home/agent/skills"
        )
        assert skill_mount.host_path == str(tmp_path / "data/personalization/skills")
        assert skill_mount.readonly is False

    def test_scheduled_agent_mounts_only_its_automation_memory(self, tmp_path: Path):
        memory_dir = tmp_path / "automation-memory/job-security"
        memory_dir.mkdir(parents=True)
        with _patch_settings(tmp_path, learning=LearningConfig(enabled=False)):
            mounts = build_volume_mounts(
                TEST_GROUP,
                is_admin=False,
                automation_memory_dir=memory_dir,
            )

        memory_mount = next(
            mount for mount in mounts if mount.container_path == "/home/agent/automation-memory"
        )
        assert memory_mount.host_path == str(memory_dir)
        assert memory_mount.readonly is False

    def test_learning_enabled_mounts_vault_readwrite(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        learning = LearningConfig(
            enabled=True,
            obsidian=ObsidianLearningConfig(
                vault_root=str(vault),
                mount_path="/mnt/obsidian",
            ),
        )

        with _patch_settings(tmp_path, learning=learning):
            (tmp_path / "groups" / "test-group").mkdir(parents=True)
            with patch(
                "pynchy.host.container_manager.mounts._configured_mount_operations",
                return_value=MountOperations(
                    prepare_agent_homes=lambda *_args: AgentHomeMounts(
                        claude_home=tmp_path / "claude",
                        codex_home=tmp_path / "codex",
                        vault_mount_root=vault.resolve(),
                        vault_mount_path="/mnt/obsidian",
                    ),
                    repo_container_path=lambda slug: f"/home/agent/src/{slug}",
                    runtime_name=lambda: "docker",
                ),
            ):
                mounts = build_volume_mounts(TEST_GROUP, is_admin=False)

        vault_mount = next(
            (m for m in mounts if m.container_path == "/mnt/obsidian"),
            None,
        )
        assert vault_mount is not None, "expected vault mount"
        assert vault_mount.host_path == str(vault.resolve())
        assert vault_mount.readonly is False

    def test_learning_mount_creates_profile_fallback_dirs(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        learning = LearningConfig(
            enabled=True,
            obsidian=ObsidianLearningConfig(vault_root=str(vault)),
        )
        profiles, workspace = _profile_workspace("Deep Work!!")
        workspaces = {"test-group": workspace}

        with _patch_settings(
            tmp_path,
            learning=learning,
            workspaces=workspaces,
        ) as settings:
            settings.profiles.update(profiles)
            (tmp_path / "groups" / "test-group").mkdir(parents=True)

            build_volume_mounts(TEST_GROUP, is_admin=False)

        profile_root = vault.resolve() / "systems/pynchy/profiles/deep-work"
        assert (profile_root / "memory").is_dir()

    def test_personalized_skill_syncs_when_profile_allows_it(
        self,
        tmp_path: Path,
    ):
        vault = tmp_path / "vault"
        vault.mkdir()
        learned_skill = tmp_path / "data/personalization/skills/remember-routing"
        learned_skill.mkdir(parents=True)
        (learned_skill / "SKILL.md").write_text(
            "---\nname: remember-routing\ntier: learned\n---\n# Remember Routing\n"
        )
        learning = LearningConfig(
            enabled=True,
            obsidian=ObsidianLearningConfig(vault_root=str(vault)),
        )
        profiles, workspace = _profile_workspace("Deep Work!!", skills=["remember-routing"])
        workspaces = {"test-group": workspace}

        with _patch_settings(
            tmp_path,
            learning=learning,
            workspaces=workspaces,
        ) as settings:
            settings.profiles.update(profiles)
            (tmp_path / "groups" / "test-group").mkdir(parents=True)

            build_volume_mounts(TEST_GROUP, is_admin=False)

        skill_dst = tmp_path / "data/sessions/test-group/.claude/skills/remember-routing/SKILL.md"
        assert skill_dst.exists()
        codex_skill_dst = (
            tmp_path / "data/sessions/test-group/.codex/skills/remember-routing" / "SKILL.md"
        )
        assert codex_skill_dst.exists()

    def test_personalized_skill_does_not_sync_without_profile_permission(
        self,
        tmp_path: Path,
    ):
        vault = tmp_path / "vault"
        vault.mkdir()
        learned_skill = tmp_path / "data/personalization/skills/remember-routing"
        learned_skill.mkdir(parents=True)
        (learned_skill / "SKILL.md").write_text(
            "---\nname: remember-routing\ntier: learned\n---\n# Remember Routing\n"
        )
        learning = LearningConfig(
            enabled=True,
            obsidian=ObsidianLearningConfig(vault_root=str(vault)),
        )
        profiles, workspace = _profile_workspace("Deep Work!!", skills=["core"])
        workspaces = {"test-group": workspace}

        with _patch_settings(
            tmp_path,
            learning=learning,
            workspaces=workspaces,
        ) as settings:
            settings.profiles.update(profiles)
            (tmp_path / "groups" / "test-group").mkdir(parents=True)

            build_volume_mounts(TEST_GROUP, is_admin=False)

        skill_dst = tmp_path / "data/sessions/test-group/.claude/skills/remember-routing/SKILL.md"
        assert not skill_dst.exists()
        codex_skill_dst = (
            tmp_path / "data/sessions/test-group/.codex/skills/remember-routing" / "SKILL.md"
        )
        assert not codex_skill_dst.exists()

    def test_refresh_personalized_agent_skills_syncs_skill_written_after_session_start(
        self,
        tmp_path: Path,
    ):
        """A warm session sees reviewer output before its next query."""
        vault = tmp_path / "vault"
        vault.mkdir()
        learning = LearningConfig(
            enabled=True,
            obsidian=ObsidianLearningConfig(vault_root=str(vault)),
        )
        profiles, workspace = _profile_workspace("Deep Work!!", skills=["core", "remember-routing"])
        workspaces = {"test-group": workspace}

        with _patch_settings(
            tmp_path,
            learning=learning,
            workspaces=workspaces,
        ) as settings:
            settings.profiles.update(profiles)

            prepare_agent_homes("test-group")
            learned_skill = tmp_path / "data/personalization/skills/remember-routing"
            learned_skill.mkdir(parents=True)
            (learned_skill / "SKILL.md").write_text(
                "---\nname: remember-routing\ntier: learned\n---\n# Remember Routing\n"
            )

            refresh_personalized_agent_skills("test-group")

        for agent_home in (".claude", ".codex"):
            skill_dst = (
                tmp_path
                / "data/sessions/test-group"
                / agent_home
                / "skills/remember-routing/SKILL.md"
            )
            assert skill_dst.exists()

    def test_warm_agent_homes_apply_new_published_skill_policy(
        self,
        tmp_path: Path,
    ) -> None:
        for name in ("alpha", "beta"):
            skill = tmp_path / "data/personalization/skills" / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: {name.title()} skill.\ntier: learned\n---\n",
                encoding="utf-8",
            )
        settings = make_settings(
            project_root=tmp_path,
            learning=LearningConfig(enabled=False),
            profiles={"base": ProfileConfig(skills=["alpha"])},
            workspaces={"test-group": WorkspaceConfig(profiles=["base"])},
        )
        publish_settings(settings)
        app = PynchyApp()
        app.sessions["test-group"] = "warm-session"

        homes = prepare_agent_homes("test-group")
        history = homes.claude_home / "history.json"
        history.write_text("preserve me", encoding="utf-8")

        updated = settings.model_copy(deep=True)
        updated.profiles["base"] = ProfileConfig(
            skills=["beta"],
            denied_skills=["alpha"],
        )
        publish_settings(updated)
        refreshed = prepare_agent_homes("test-group")

        assert refreshed == homes
        assert history.read_text(encoding="utf-8") == "preserve me"
        assert app.sessions["test-group"] == "warm-session"
        for agent_home in (homes.claude_home, homes.codex_home):
            assert not (agent_home / "skills/alpha").exists()
            assert (agent_home / "skills/beta/SKILL.md").is_file()

    def test_host_execution_syncs_personalized_skill_into_its_isolated_codex_home(
        self,
        tmp_path: Path,
    ):
        vault = tmp_path / "vault"
        vault.mkdir()
        learned_skill = tmp_path / "data/personalization/skills/remember-routing"
        learned_skill.mkdir(parents=True)
        (learned_skill / "SKILL.md").write_text(
            "---\nname: remember-routing\ntier: learned\n---\n# Remember Routing\n"
        )
        learning = LearningConfig(
            enabled=True,
            obsidian=ObsidianLearningConfig(vault_root=str(vault)),
        )
        profiles, workspace = _profile_workspace("Deep Work!!", skills=["core", "remember-routing"])
        workspaces = {"test-group": workspace}

        with _patch_settings(
            tmp_path,
            learning=learning,
            workspaces=workspaces,
        ) as settings:
            settings.profiles.update(profiles)
            operations = make_host_runtime_operations()
            operations.sessions_root = settings.data_dir / "sessions"
            operations.project_root = settings.project_root
            operations.gateway_port = settings.gateway.port
            operations.prepare_host_codex_home = lambda folder, plugins: (
                prepare_agent_homes(folder, plugins).codex_home
            )
            codex_home = host_execution.prepare_host_codex_home("test-group", None, operations)
            env = host_execution.host_agent_env_vars(
                is_admin=False,
                group_folder="test-group",
                operations=operations,
                codex_home=codex_home,
            )

        assert codex_home == tmp_path / "data/sessions/test-group/.codex"
        assert env["CODEX_HOME"] == str(codex_home)
        assert (codex_home / "skills/remember-routing/SKILL.md").exists()

    def test_personalized_skill_syncs_when_named(
        self,
        tmp_path: Path,
    ):
        vault = tmp_path / "vault"
        vault.mkdir()
        learned_skill = tmp_path / "data/personalization/skills/remember-routing"
        learned_skill.mkdir(parents=True)
        (learned_skill / "SKILL.md").write_text(
            "---\nname: remember-routing\ntier: learned\n---\n# Remember Routing\n"
        )
        learning = LearningConfig(
            enabled=True,
            obsidian=ObsidianLearningConfig(vault_root=str(vault)),
        )
        profiles, workspace = _profile_workspace("Deep Work!!", skills=["remember-routing"])
        workspaces = {"test-group": workspace}

        with _patch_settings(
            tmp_path,
            learning=learning,
            workspaces=workspaces,
        ) as settings:
            settings.profiles.update(profiles)
            (tmp_path / "groups" / "test-group").mkdir(parents=True)

            build_volume_mounts(TEST_GROUP, is_admin=False)

        skill_dst = tmp_path / "data/sessions/test-group/.claude/skills/remember-routing/SKILL.md"
        assert skill_dst.exists()

    def test_codex_home_receives_selected_plugin_skills(self, tmp_path: Path):
        plugin_skill = tmp_path / "vault-skills" / "calendar-caldav"
        plugin_skill.mkdir(parents=True)
        (plugin_skill / "SKILL.md").write_text(
            "---\nname: calendar-caldav\ntier: community\n---\n# Calendar\n"
        )

        class FakeHook:
            def pynchy_skill_paths(self):
                return [[str(plugin_skill)]]

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

        profiles, workspace = _profile_workspace(skills=["calendar-caldav"])
        workspaces = {"test-group": workspace}
        with _patch_settings(tmp_path, workspaces=workspaces) as settings:
            settings.profiles.update(profiles)
            (tmp_path / "groups" / "test-group").mkdir(parents=True)

            build_volume_mounts(TEST_GROUP, is_admin=False, plugin_manager=FakePM())

        claude_skill = tmp_path / "data/sessions/test-group/.claude/skills/calendar-caldav/SKILL.md"
        codex_skill = tmp_path / "data/sessions/test-group/.codex/skills/calendar-caldav/SKILL.md"
        assert claude_skill.exists()
        assert codex_skill.read_text() == claude_skill.read_text()

    @pytest.mark.parametrize("vault_state", ["missing", "file"])
    def test_learning_enabled_requires_existing_vault_directory(
        self,
        tmp_path: Path,
        vault_state: str,
    ):
        vault = tmp_path / "vault"
        if vault_state == "file":
            vault.write_text("not a directory")
        learning = LearningConfig(
            enabled=True,
            obsidian=ObsidianLearningConfig(vault_root=str(vault)),
        )

        (tmp_path / "groups" / "test-group").mkdir(parents=True)
        with (
            _patch_settings(tmp_path, learning=learning),
            pytest.raises(LearningConfigError, match=r"vault_root.*directory"),
        ):
            build_volume_mounts(TEST_GROUP, is_admin=False)

    def test_admin_group_has_repo_mount(self, tmp_path: Path):
        worktree_path = tmp_path / "worktrees" / "admin-1"
        worktree_path.mkdir(parents=True)
        repo_ctx = RepoContext(
            slug="owner/pynchy", root=tmp_path, worktrees_dir=tmp_path / "worktrees"
        )
        with (
            _patch_settings(tmp_path),
        ):
            (tmp_path / "groups" / "admin-1").mkdir(parents=True)
            group = WorkspaceProfile(
                jid="admin-1@g.us",
                name="Admin",
                folder="admin-1",
                trigger="always",
                added_at="2024-01-01",
            )
            mounts = build_volume_mounts(
                group, is_admin=True, repo_ctx=repo_ctx, worktree_path=worktree_path
            )

            paths = [m.container_path for m in mounts]
            assert "/home/agent/src/owner/pynchy" in paths
            assert "/home/agent/workspace" in paths
            assert "/home/agent/global" not in paths

    def test_nonadmin_group_has_no_global_mount(self, tmp_path: Path):
        """Non-admin groups no longer get a global mount.

        Directives replaced the old global CLAUDE.md overlay — content is now
        resolved host-side and passed via system_prompt_append.
        """
        with (
            _patch_settings(tmp_path),
        ):
            (tmp_path / "groups" / "other").mkdir(parents=True)
            (tmp_path / "groups" / "global").mkdir(parents=True)
            group = WorkspaceProfile(
                jid="other@g.us",
                name="Other",
                folder="other",
                trigger="@pynchy",
                added_at="2024-01-01",
            )
            mounts = build_volume_mounts(group, is_admin=False)

            paths = [m.container_path for m in mounts]
            assert "/home/agent/src/owner/pynchy" not in paths
            assert "/home/agent/workspace" in paths
            assert "/home/agent/global" not in paths

    def test_nonadmin_repo_access_uses_worktree_path(self, tmp_path: Path):
        """Non-admin group with repo access mounts the worktree under /home/agent/src."""
        worktree_path = tmp_path / "worktrees" / "code-improver"
        worktree_path.mkdir(parents=True)
        repo_ctx = RepoContext(
            slug="owner/pynchy", root=tmp_path, worktrees_dir=tmp_path / "worktrees"
        )

        with (
            _patch_settings(tmp_path),
        ):
            (tmp_path / "groups" / "code-improver").mkdir(parents=True)
            group = WorkspaceProfile(
                jid="code-improver@g.us",
                name="Code Improver",
                folder="code-improver",
                trigger="@pynchy",
                added_at="2024-01-01",
            )
            mounts = build_volume_mounts(
                group, is_admin=False, repo_ctx=repo_ctx, worktree_path=worktree_path
            )

            repo_mount = next(
                m for m in mounts if m.container_path == "/home/agent/src/owner/pynchy"
            )
            assert repo_mount.host_path == str(worktree_path)
            assert repo_mount.readonly is False

            # .git dir mounted at host path so worktree gitdir reference resolves
            git_mount = next(m for m in mounts if m.host_path == str(tmp_path / ".git"))
            assert git_mount.container_path == str(tmp_path / ".git")

    def test_admin_uses_worktree(self, tmp_path: Path):
        """Admin group uses worktree just like any other repo_access group."""
        worktree_path = tmp_path / "worktrees" / "admin-1"
        worktree_path.mkdir(parents=True)
        repo_ctx = RepoContext(
            slug="owner/pynchy", root=tmp_path, worktrees_dir=tmp_path / "worktrees"
        )
        with (
            _patch_settings(tmp_path),
        ):
            (tmp_path / "groups" / "admin-1").mkdir(parents=True)
            group = WorkspaceProfile(
                jid="admin-1@g.us",
                name="Admin",
                folder="admin-1",
                trigger="always",
                added_at="2024-01-01",
            )
            mounts = build_volume_mounts(
                group, is_admin=True, repo_ctx=repo_ctx, worktree_path=worktree_path
            )

            repo_mount = next(
                m for m in mounts if m.container_path == "/home/agent/src/owner/pynchy"
            )
            assert repo_mount.host_path == str(worktree_path)
            assert repo_mount.readonly is False

    def test_admin_gets_raw_host_repo_mount(self, tmp_path: Path):
        """Admin group gets a raw host repo mount when repo_ctx is provided."""
        worktree_path = tmp_path / "worktrees" / "admin-1"
        worktree_path.mkdir(parents=True)
        repo_ctx = RepoContext(
            slug="owner/pynchy", root=tmp_path, worktrees_dir=tmp_path / "worktrees"
        )
        with _patch_settings(tmp_path):
            (tmp_path / "groups" / "admin-1").mkdir(parents=True)
            group = WorkspaceProfile(
                jid="admin-1@g.us",
                name="Admin",
                folder="admin-1",
                trigger="always",
                added_at="2024-01-01",
            )
            mounts = build_volume_mounts(
                group, is_admin=True, repo_ctx=repo_ctx, worktree_path=worktree_path
            )

            raw_mount = next(
                m for m in mounts if m.container_path == "/danger/raw-host-repos/owner/pynchy"
            )
            assert raw_mount.host_path == str(tmp_path)
            assert raw_mount.readonly is False
