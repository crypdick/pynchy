"""Tests for skill plugin system."""

from __future__ import annotations

from pathlib import Path

import pytest

from pynchy.plugin import SkillPlugin


class MockSkillPlugin(SkillPlugin):
    """Mock skill plugin for testing."""

    def __init__(self, name: str = "test-skill", skill_paths: list[Path] | None = None):
        self.name = name
        self.version = "0.1.0"
        self.categories = ["skill"]
        self.description = "Test skill plugin"
        self._skill_paths = skill_paths if skill_paths is not None else []

    def skill_paths(self) -> list[Path]:
        """Return configured skill paths."""
        return self._skill_paths


class TestSkillPlugin:
    """Tests for SkillPlugin base class."""

    def test_skill_plugin_has_fixed_category(self):
        """SkillPlugin has fixed 'skill' category."""
        assert SkillPlugin.categories == ["skill"]

    def test_skill_plugin_requires_skill_paths_method(self):
        """SkillPlugin requires implementation of skill_paths."""
        # SkillPlugin is abstract - can't instantiate directly
        with pytest.raises(TypeError):
            SkillPlugin()  # type: ignore

    def test_mock_skill_plugin_validates(self):
        """MockSkillPlugin passes validation."""
        plugin = MockSkillPlugin(name="test", skill_paths=[])
        plugin.validate()  # Should not raise

    def test_skill_plugin_returns_path_list(self):
        """skill_paths returns a list of Path objects."""
        test_paths = [Path("/tmp/skill1"), Path("/tmp/skill2")]
        plugin = MockSkillPlugin(name="test", skill_paths=test_paths)

        paths = plugin.skill_paths()

        assert isinstance(paths, list)
        assert len(paths) == 2
        assert all(isinstance(p, Path) for p in paths)
        assert paths == test_paths

    def test_skill_plugin_can_return_empty_list(self):
        """skill_paths can return empty list."""
        plugin = MockSkillPlugin(name="test", skill_paths=[])
        paths = plugin.skill_paths()
        assert paths == []


class TestSkillPluginIntegration:
    """Integration tests for skill plugin system."""

    def test_skill_plugin_with_real_path(self, tmp_path: Path):
        """SkillPlugin can reference real filesystem paths."""
        # Create a test skill directory
        skill_dir = tmp_path / "test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Test Skill\n\nThis is a test.")

        plugin = MockSkillPlugin(name="test", skill_paths=[skill_dir])
        paths = plugin.skill_paths()

        assert len(paths) == 1
        assert paths[0] == skill_dir
        assert paths[0].exists()
        assert (paths[0] / "SKILL.md").exists()

    def test_skill_plugin_with_multiple_skills(self, tmp_path: Path):
        """SkillPlugin can provide multiple skills."""
        skill1 = tmp_path / "skill1"
        skill1.mkdir()
        (skill1 / "SKILL.md").write_text("# Skill 1")

        skill2 = tmp_path / "skill2"
        skill2.mkdir()
        (skill2 / "SKILL.md").write_text("# Skill 2")

        plugin = MockSkillPlugin(name="multi", skill_paths=[skill1, skill2])
        paths = plugin.skill_paths()

        assert len(paths) == 2
        assert skill1 in paths
        assert skill2 in paths
