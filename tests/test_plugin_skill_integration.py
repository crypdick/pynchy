"""Integration tests for skill plugin syncing to container sessions."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from pynchy.container_runner import _sync_skills
from pynchy.plugin import PluginRegistry


class MockSkillPlugin:
    """Mock skill plugin for testing."""

    def __init__(self, name: str, skill_paths: list[Path]):
        self.name = name
        self.version = "0.1.0"
        self.categories = ["skill"]
        self._skill_paths = skill_paths

    def skill_paths(self) -> list[Path]:
        return self._skill_paths


class TestSkillSyncing:
    """Tests for _sync_skills function with plugin support."""

    def test_sync_skills_creates_skills_directory(self, tmp_path: Path):
        """_sync_skills creates the skills directory if it doesn't exist."""
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        _sync_skills(session_dir)

        skills_dir = session_dir / "skills"
        assert skills_dir.exists()
        assert skills_dir.is_dir()

    def test_sync_skills_without_registry(self, tmp_path: Path):
        """_sync_skills works without a registry (no plugins)."""
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        # Should not raise
        _sync_skills(session_dir, registry=None)

        assert (session_dir / "skills").exists()

    def test_sync_skills_with_empty_registry(self, tmp_path: Path):
        """_sync_skills works with empty registry."""
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        registry = PluginRegistry()

        _sync_skills(session_dir, registry=registry)

        assert (session_dir / "skills").exists()

    def test_sync_skills_copies_plugin_skill(self, tmp_path: Path):
        """_sync_skills copies skill from plugin."""
        # Create plugin skill
        plugin_skill_dir = tmp_path / "plugin_skills" / "test-skill"
        plugin_skill_dir.mkdir(parents=True)
        (plugin_skill_dir / "SKILL.md").write_text("# Test Skill")
        (plugin_skill_dir / "example.txt").write_text("Example content")

        # Create session dir
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        # Create registry with plugin
        registry = PluginRegistry()
        plugin = MockSkillPlugin(name="test-plugin", skill_paths=[plugin_skill_dir])
        registry.skills.append(plugin)

        _sync_skills(session_dir, registry=registry)

        # Verify skill was copied
        synced_skill = session_dir / "skills" / "test-skill"
        assert synced_skill.exists()
        assert (synced_skill / "SKILL.md").exists()
        assert (synced_skill / "example.txt").exists()
        assert (synced_skill / "SKILL.md").read_text() == "# Test Skill"
        assert (synced_skill / "example.txt").read_text() == "Example content"

    def test_sync_skills_copies_multiple_plugin_skills(self, tmp_path: Path):
        """_sync_skills copies multiple skills from a plugin."""
        # Create plugin skills
        skill1_dir = tmp_path / "plugin_skills" / "skill1"
        skill1_dir.mkdir(parents=True)
        (skill1_dir / "SKILL.md").write_text("# Skill 1")

        skill2_dir = tmp_path / "plugin_skills" / "skill2"
        skill2_dir.mkdir(parents=True)
        (skill2_dir / "SKILL.md").write_text("# Skill 2")

        # Create session dir
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        # Create registry with plugin
        registry = PluginRegistry()
        plugin = MockSkillPlugin(name="multi-skill", skill_paths=[skill1_dir, skill2_dir])
        registry.skills.append(plugin)

        _sync_skills(session_dir, registry=registry)

        # Verify both skills were copied
        assert (session_dir / "skills" / "skill1" / "SKILL.md").exists()
        assert (session_dir / "skills" / "skill2" / "SKILL.md").exists()

    def test_sync_skills_with_multiple_plugins(self, tmp_path: Path):
        """_sync_skills copies skills from multiple plugins."""
        # Create skills from different plugins
        plugin1_skill = tmp_path / "plugin1" / "skill-a"
        plugin1_skill.mkdir(parents=True)
        (plugin1_skill / "SKILL.md").write_text("# Skill A")

        plugin2_skill = tmp_path / "plugin2" / "skill-b"
        plugin2_skill.mkdir(parents=True)
        (plugin2_skill / "SKILL.md").write_text("# Skill B")

        # Create session dir
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        # Create registry with multiple plugins
        registry = PluginRegistry()
        plugin1 = MockSkillPlugin(name="plugin1", skill_paths=[plugin1_skill])
        plugin2 = MockSkillPlugin(name="plugin2", skill_paths=[plugin2_skill])
        registry.skills.extend([plugin1, plugin2])

        _sync_skills(session_dir, registry=registry)

        # Verify skills from both plugins were copied
        assert (session_dir / "skills" / "skill-a" / "SKILL.md").exists()
        assert (session_dir / "skills" / "skill-b" / "SKILL.md").exists()

    def test_sync_skills_handles_nonexistent_path(self, tmp_path: Path):
        """_sync_skills handles plugin returning nonexistent path."""
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        # Plugin returns path that doesn't exist
        nonexistent_path = tmp_path / "nonexistent" / "skill"
        registry = PluginRegistry()
        plugin = MockSkillPlugin(name="broken", skill_paths=[nonexistent_path])
        registry.skills.append(plugin)

        # Should not raise, just log warning
        _sync_skills(session_dir, registry=registry)

        # Skill directory should not exist
        assert not (session_dir / "skills" / "skill").exists()

    def test_sync_skills_handles_file_instead_of_directory(self, tmp_path: Path):
        """_sync_skills handles plugin returning file instead of directory."""
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        # Plugin returns a file path instead of directory
        file_path = tmp_path / "not_a_dir.txt"
        file_path.write_text("Not a directory")
        registry = PluginRegistry()
        plugin = MockSkillPlugin(name="broken", skill_paths=[file_path])
        registry.skills.append(plugin)

        # Should not raise, just log warning
        _sync_skills(session_dir, registry=registry)

    def test_sync_skills_handles_plugin_exception(self, tmp_path: Path):
        """_sync_skills handles exception from plugin.skill_paths()."""
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        # Plugin that raises exception
        broken_plugin = Mock()
        broken_plugin.name = "broken"
        broken_plugin.skill_paths.side_effect = RuntimeError("Plugin error")

        registry = PluginRegistry()
        registry.skills.append(broken_plugin)

        # Should not raise, just log warning
        _sync_skills(session_dir, registry=registry)

    def test_sync_skills_raises_on_builtin_name_collision(self, tmp_path: Path):
        """_sync_skills raises ValueError when plugin skill collides with built-in skill."""
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        # Plugin tries to provide a skill named "agent-browser" — same as the built-in.
        # _sync_skills copies built-ins first, so the collision is detected on plugin copy.
        plugin_skill = tmp_path / "plugin" / "agent-browser"
        plugin_skill.mkdir(parents=True)
        (plugin_skill / "SKILL.md").write_text("# Malicious override")

        registry = PluginRegistry()
        plugin = MockSkillPlugin(name="evil-plugin", skill_paths=[plugin_skill])
        registry.skills.append(plugin)

        with pytest.raises(ValueError, match="Skill name collision.*evil-plugin.*agent-browser"):
            _sync_skills(session_dir, registry=registry)

    def test_sync_skills_raises_on_plugin_vs_plugin_collision(self, tmp_path: Path):
        """_sync_skills raises ValueError when two plugins provide the same skill name."""
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        # Two plugins provide a skill called "shared-name"
        skill1 = tmp_path / "plugin1" / "shared-name"
        skill1.mkdir(parents=True)
        (skill1 / "SKILL.md").write_text("# From plugin 1")

        skill2 = tmp_path / "plugin2" / "shared-name"
        skill2.mkdir(parents=True)
        (skill2 / "SKILL.md").write_text("# From plugin 2")

        registry = PluginRegistry()
        registry.skills.extend([
            MockSkillPlugin(name="plugin-a", skill_paths=[skill1]),
            MockSkillPlugin(name="plugin-b", skill_paths=[skill2]),
        ])

        with pytest.raises(ValueError, match="Skill name collision.*plugin-b.*shared-name"):
            _sync_skills(session_dir, registry=registry)

    def test_sync_skills_preserves_directory_structure(self, tmp_path: Path):
        """_sync_skills preserves subdirectory structure in skills."""
        # Create plugin skill with subdirectories
        plugin_skill = tmp_path / "plugin" / "complex-skill"
        plugin_skill.mkdir(parents=True)
        (plugin_skill / "SKILL.md").write_text("# Complex Skill")
        subdir = plugin_skill / "examples"
        subdir.mkdir()
        (subdir / "example.md").write_text("Example")

        # Create session dir
        session_dir = tmp_path / "session"
        session_dir.mkdir()

        registry = PluginRegistry()
        plugin = MockSkillPlugin(name="complex", skill_paths=[plugin_skill])
        registry.skills.append(plugin)

        _sync_skills(session_dir, registry=registry)

        # Verify structure preserved
        synced = session_dir / "skills" / "complex-skill"
        assert (synced / "SKILL.md").exists()
        assert (synced / "examples" / "example.md").exists()
        assert (synced / "examples" / "example.md").read_text() == "Example"
