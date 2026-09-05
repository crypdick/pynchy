"""Tests for the container runner."""

from __future__ import annotations

import logging
import shutil
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import pluggy
import pytest

from pynchy.agent_home import (
    is_skill_selected,
    parse_skill_tier,
    refresh_personalized_skills,
    sync_skills,
)
from pynchy.workspace.api import (
    WorkspaceProfile,
)
from tests.container_runner_support import (
    _patch_settings,
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


class TestSyncSkills:
    """Test skill syncing from configured defaults and plugins into session dirs."""

    def test_copies_default_skills(self, tmp_path: Path):
        """Default skills are copied to the session .claude/skills/ dir."""
        default_skill = tmp_path / "data" / "defaults" / "skills" / "my-skill"
        default_skill.mkdir(parents=True)
        (default_skill / "skill.md").write_text("# My Skill\nDo stuff.")
        (default_skill / "config.json").write_text('{"name": "my-skill"}')

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            sync_skills(session_dir, project_root=tmp_path, workspace_skills=["*"])

        skills_dst = session_dir / "skills" / "my-skill"
        assert skills_dst.exists()
        assert (skills_dst / "skill.md").read_text() == "# My Skill\nDo stuff."
        assert (skills_dst / "config.json").exists()

    def test_no_default_skills_dir_is_safe(self, tmp_path: Path):
        """Missing default skills directory should not crash."""
        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            sync_skills(session_dir, project_root=tmp_path)

        # skills/ directory should still be created (empty)
        assert (session_dir / "skills").exists()

    def test_plugin_skills_are_synced(self, tmp_path: Path):
        """Plugin manager skill paths are copied to session dir."""
        plugin_skill = tmp_path / "plugins" / "ext-skill"
        plugin_skill.mkdir(parents=True)
        (plugin_skill / "skill.md").write_text("# External Skill")

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        class FakeHook:
            def pynchy_skill_paths(self):
                return [[str(plugin_skill)]]

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

        with _patch_settings(tmp_path):
            sync_skills(
                session_dir, project_root=tmp_path, plugin_manager=FakePM(), workspace_skills=["*"]
            )

        ext_dst = session_dir / "skills" / "ext-skill"
        assert ext_dst.exists()
        assert (ext_dst / "skill.md").read_text() == "# External Skill"

    def test_plugin_skills_are_resynced(self, tmp_path: Path):
        """Repeated syncs update plugin skills in persistent session dirs."""
        plugin_skill = tmp_path / "plugins" / "ext-skill"
        plugin_skill.mkdir(parents=True)
        skill_md = plugin_skill / "SKILL.md"
        skill_md.write_text("# External Skill\nfirst")

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        class FakeHook:
            def pynchy_skill_paths(self):
                return [[str(plugin_skill)]]

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

        with _patch_settings(tmp_path):
            sync_skills(
                session_dir, project_root=tmp_path, plugin_manager=FakePM(), workspace_skills=["*"]
            )
            skill_md.write_text("# External Skill\nsecond")
            sync_skills(
                session_dir, project_root=tmp_path, plugin_manager=FakePM(), workspace_skills=["*"]
            )

        ext_dst = session_dir / "skills" / "ext-skill"
        assert (ext_dst / "SKILL.md").read_text() == "# External Skill\nsecond"

    def test_plugin_skill_survives_site_packages_prefix_relocation(self, tmp_path: Path):
        plugin_skill = tmp_path / "new/lib/python3.13/site-packages/example_plugin/skills/ext-skill"
        plugin_skill.mkdir(parents=True)
        (plugin_skill / "SKILL.md").write_text("# Current skill\n")

        session_dir = tmp_path / "session/.claude"
        destination = session_dir / "skills/ext-skill"
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text("# Prior skill\n")
        old_source = tmp_path / "old/site-packages/example_plugin/skills/ext-skill"
        (destination / ".pynchy-plugin-skill").write_text(f"{old_source}\n")

        class FakeHook:
            def pynchy_skill_paths(self):
                return [[str(plugin_skill)]]

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

        with _patch_settings(tmp_path):
            sync_skills(
                session_dir,
                project_root=tmp_path,
                plugin_manager=FakePM(),
                workspace_skills=["*"],
            )

        assert (destination / "SKILL.md").read_text() == "# Current skill\n"
        assert (destination / ".pynchy-plugin-skill").read_text().strip() == str(
            plugin_skill.resolve()
        )

    def test_unmarked_prior_plugin_skill_copy_is_resynced(self, tmp_path: Path):
        """Old plugin copies without markers are upgraded on the next sync."""
        plugin_skill = tmp_path / "plugins" / "ext-skill"
        plugin_skill.mkdir(parents=True)
        skill_md = plugin_skill / "SKILL.md"
        skill_md.write_text("# External Skill\nsecond")

        session_dir = tmp_path / "session" / ".claude"
        ext_dst = session_dir / "skills" / "ext-skill"
        ext_dst.mkdir(parents=True)
        (ext_dst / "SKILL.md").write_text("# External Skill\nfirst")

        class FakeHook:
            def pynchy_skill_paths(self):
                return [[str(plugin_skill)]]

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

        with _patch_settings(tmp_path):
            sync_skills(
                session_dir, project_root=tmp_path, plugin_manager=FakePM(), workspace_skills=["*"]
            )

        assert (ext_dst / "SKILL.md").read_text() == "# External Skill\nsecond"

    def test_bad_plugin_skill_path_does_not_block_later_plugin_skill(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ):
        """One malformed plugin path should not prevent later plugin skills from syncing."""
        plugin_skill = tmp_path / "plugins" / "ext-skill"
        plugin_skill.mkdir(parents=True)
        (plugin_skill / "skill.md").write_text("# External Skill")

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        class FakeHook:
            def pynchy_skill_paths(self):
                return [[None, str(plugin_skill)]]

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

        caplog.set_level(logging.ERROR)
        with _patch_settings(tmp_path):
            sync_skills(
                session_dir, project_root=tmp_path, plugin_manager=FakePM(), workspace_skills=["*"]
            )

        ext_dst = session_dir / "skills" / "ext-skill"
        assert ext_dst.exists()
        assert "Failed to sync plugin skill" in caplog.text

    def test_plugin_skill_name_collision_raises(self, tmp_path: Path):
        """Plugin skill that shadows a default skill raises ValueError."""
        default_skill = tmp_path / "data" / "defaults" / "skills" / "my-skill"
        default_skill.mkdir(parents=True)
        (default_skill / "skill.md").write_text("default")

        # Create plugin skill with same name
        plugin_skill = tmp_path / "plugins" / "my-skill"
        plugin_skill.mkdir(parents=True)
        (plugin_skill / "skill.md").write_text("plugin")

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        class FakeHook:
            def pynchy_skill_paths(self):
                return [[str(plugin_skill)]]

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

        with (
            _patch_settings(tmp_path),
            pytest.raises(ValueError, match="collision"),
        ):
            sync_skills(
                session_dir, project_root=tmp_path, plugin_manager=FakePM(), workspace_skills=["*"]
            )

    def test_personalized_skill_overrides_plugin_skill(self, tmp_path: Path):
        plugin_skill = tmp_path / "plugins/my-skill"
        plugin_skill.mkdir(parents=True)
        (plugin_skill / "SKILL.md").write_text("plugin")

        class FakeHook:
            def pynchy_skill_paths(self):
                return [[str(plugin_skill)]]

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

        session_dir = tmp_path / "session/.claude"
        with _patch_settings(tmp_path):
            sync_skills(
                session_dir,
                project_root=tmp_path,
                plugin_manager=FakePM(),
                workspace_skills=["*"],
            )
            personalized = tmp_path / "data/personalization/skills/my-skill"
            personalized.mkdir(parents=True)
            (personalized / "SKILL.md").write_text(
                "---\nname: my-skill\ntier: learned\n---\npersonalized"
            )
            sync_skills(
                session_dir,
                project_root=tmp_path,
                plugin_manager=FakePM(),
                workspace_skills=["*"],
            )

        assert "personalized" in (session_dir / "skills/my-skill/SKILL.md").read_text()

    def test_skips_nonexistent_plugin_skill_path(self, tmp_path: Path):
        """Plugin skill paths that don't exist are skipped with a warning."""
        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        class FakeHook:
            def pynchy_skill_paths(self):
                return [[str(tmp_path / "nonexistent-skill")]]

        class FakePM(pluggy.PluginManager):
            hook = FakeHook()

            def __init__(self):
                pass

        with _patch_settings(tmp_path):
            # Should not crash
            sync_skills(session_dir, project_root=tmp_path, plugin_manager=FakePM())

    def test_ignores_files_in_default_skills_dir(self, tmp_path: Path):
        """Files (not directories) in default skills are ignored."""
        skills_dir = tmp_path / "data" / "defaults" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "README.md").write_text("not a skill dir")

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            sync_skills(session_dir, project_root=tmp_path)

        # Only the skills/ directory should exist, no README.md copied
        assert not (session_dir / "skills" / "README.md").exists()

    def test_does_not_load_legacy_agent_skill_path(self, tmp_path: Path):
        legacy_skill = tmp_path / "src" / "pynchy" / "agent" / "skills" / "legacy"
        legacy_skill.mkdir(parents=True)
        (legacy_skill / "SKILL.md").write_text("# Legacy")

        session_dir = tmp_path / "session" / ".claude"
        session_dir.mkdir(parents=True)

        with _patch_settings(tmp_path):
            sync_skills(session_dir, project_root=tmp_path, workspace_skills=["*"])

        assert not (session_dir / "skills" / "legacy").exists()

    def test_personalized_skill_syncs_full_tree(self, tmp_path: Path):
        skill = tmp_path / "data/personalization/skills/remember-routing"
        references = skill / "references"
        references.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: remember-routing\ntier: learned\n---\n# Remember Routing\n"
        )
        (references / "runbook.md").write_text("Use the right queue.")
        session_dir = tmp_path / "session/.claude"

        with _patch_settings(tmp_path):
            sync_skills(
                session_dir,
                project_root=tmp_path,
                workspace_skills=["learned"],
            )

        copied = session_dir / "skills/remember-routing"
        assert (copied / "references/runbook.md").read_text() == "Use the right queue."
        assert (copied / ".pynchy-personalized-skill").is_file()

    def test_denied_personalized_skill_is_not_injected(self, tmp_path: Path):
        skill = tmp_path / "data/personalization/skills/obsidian-filer"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: obsidian-filer\ntier: learned\n---\n# Obsidian Filer\n"
        )
        session_dir = tmp_path / "session/.codex"

        with _patch_settings(tmp_path):
            sync_skills(
                session_dir,
                project_root=tmp_path,
                workspace_skills=["*"],
                denied_skill_names=["obsidian-filer"],
            )

        assert not (session_dir / "skills/obsidian-filer").exists()

    def test_personalized_skill_overrides_public_default(self, tmp_path: Path):
        default = tmp_path / "data/defaults/skills/shared-name"
        personalized = tmp_path / "data/personalization/skills/shared-name"
        default.mkdir(parents=True)
        personalized.mkdir(parents=True)
        (default / "SKILL.md").write_text("default")
        (personalized / "SKILL.md").write_text(
            "---\nname: shared-name\ntier: learned\n---\npersonalized"
        )
        session_dir = tmp_path / "session/.claude"

        with _patch_settings(tmp_path):
            sync_skills(session_dir, project_root=tmp_path, workspace_skills=["*"])

        copied = session_dir / "skills/shared-name/SKILL.md"
        assert "personalized" in copied.read_text()

    def test_personalized_skill_refresh_updates_and_prunes(self, tmp_path: Path):
        skill = tmp_path / "data/personalization/skills/remember-routing"
        skill.mkdir(parents=True)
        skill_md = skill / "SKILL.md"
        skill_md.write_text("---\nname: remember-routing\ntier: learned\n---\n# First Version\n")
        session_dir = tmp_path / "session/.claude"

        with _patch_settings(tmp_path):
            sync_skills(session_dir, project_root=tmp_path, workspace_skills=["learned"])
            skill_md.write_text(
                "---\nname: remember-routing\ntier: learned\n---\n# Updated Version\n"
            )
            refresh_personalized_skills(
                session_dir,
                project_root=tmp_path,
                workspace_skills=["learned"],
                denied_skill_names=None,
            )
            assert (
                "# Updated Version"
                in (session_dir / "skills/remember-routing/SKILL.md").read_text()
            )

            shutil.rmtree(skill)
            refresh_personalized_skills(
                session_dir,
                project_root=tmp_path,
                workspace_skills=["learned"],
                denied_skill_names=None,
            )

        assert not (session_dir / "skills/remember-routing").exists()

    def test_personalized_skill_with_symlink_is_not_injected(self, tmp_path: Path):
        skill = tmp_path / "data/personalization/skills/linked"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: linked\ntier: learned\n---\n")
        (skill / "outside").symlink_to(tmp_path / "outside")
        session_dir = tmp_path / "session/.claude"

        with _patch_settings(tmp_path):
            sync_skills(session_dir, project_root=tmp_path, workspace_skills=["learned"])

        assert not (session_dir / "skills/linked").exists()

    def test_personalized_skill_does_not_replace_session_symlink(self, tmp_path: Path):
        skill = tmp_path / "data/personalization/skills/linked"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: linked\ntier: learned\n---\n")
        session_skill = tmp_path / "session/.claude/skills/linked"
        session_skill.parent.mkdir(parents=True)
        session_skill.symlink_to(tmp_path / "outside")

        with (
            _patch_settings(tmp_path),
            pytest.raises(ValueError, match="collision"),
        ):
            sync_skills(
                tmp_path / "session/.claude",
                project_root=tmp_path,
                workspace_skills=["learned"],
            )


class TestParseSkillTier:
    """Test SKILL.md frontmatter parsing for name and tier."""

    def test_valid_frontmatter(self, tmp_path: Path):
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: my-skill\ntier: core\n---\n# My Skill\n")
        name, tier = parse_skill_tier(skill_dir)
        assert name == "my-skill"
        assert tier == "core"

    def test_missing_tier_defaults_to_community(self, tmp_path: Path):
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: my-skill\n---\n# My Skill\n")
        name, tier = parse_skill_tier(skill_dir)
        assert name == "my-skill"
        assert tier == "community"

    def test_no_skill_md_defaults(self, tmp_path: Path):
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        name, tier = parse_skill_tier(skill_dir)
        assert name == "my-skill"
        assert tier == "community"

    def test_no_frontmatter_delimiters(self, tmp_path: Path):
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Just a heading\nNo frontmatter here.\n")
        name, tier = parse_skill_tier(skill_dir)
        assert name == "my-skill"
        assert tier == "community"

    def test_dev_tier(self, tmp_path: Path):
        skill_dir = tmp_path / "code-improver"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: code-improver\ntier: dev\n---\n# Code Improver\n"
        )
        name, tier = parse_skill_tier(skill_dir)
        assert name == "code-improver"
        assert tier == "dev"

    def test_name_defaults_to_dir_name(self, tmp_path: Path):
        """When name is missing from frontmatter, use directory name."""
        skill_dir = tmp_path / "web-search"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\ntier: core\n---\n# Web Search\n")
        name, tier = parse_skill_tier(skill_dir)
        assert name == "web-search"
        assert tier == "core"

    def test_unclosed_frontmatter_keeps_parsed_metadata(self, tmp_path: Path):
        skill_dir = tmp_path / "web-search"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: custom\ntier: core\n")

        assert parse_skill_tier(skill_dir) == ("custom", "core")


class TestIsSkillSelected:
    """Test skill selection resolution logic."""

    def test_none_is_core_only(self):
        """skills=None means core-only (safe default)."""
        assert is_skill_selected("any-skill", "community", None) is False
        assert is_skill_selected("browser", "core", None) is True

    def test_star_includes_everything(self):
        assert is_skill_selected("any-skill", "community", ["*"]) is True

    def test_tier_match(self):
        assert is_skill_selected("my-skill", "dev", ["dev"]) is True

    def test_name_match(self):
        assert is_skill_selected("web-search", "community", ["web-search"]) is True

    def test_core_always_included_when_filtering_active(self):
        """Core tier is implicit when any filtering is set."""
        assert is_skill_selected("browser", "core", ["dev"]) is True

    def test_community_excluded_when_not_listed(self):
        assert is_skill_selected("some-skill", "community", ["core"]) is False

    def test_dev_excluded_when_not_listed(self):
        assert is_skill_selected("code-improver", "dev", ["core"]) is False

    def test_union_of_tier_and_name(self):
        """Tiers and names are unioned."""
        ws = ["core", "web-search"]
        assert is_skill_selected("web-search", "community", ws) is True
        assert is_skill_selected("python-heredoc", "core", ws) is True
        assert is_skill_selected("code-improver", "dev", ws) is False

    def test_empty_list_still_includes_core(self):
        """Even an empty skills list includes core (filtering is active)."""
        assert is_skill_selected("browser", "core", []) is True
        assert is_skill_selected("other", "community", []) is False
