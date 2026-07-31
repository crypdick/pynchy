"""Public skill synchronization security boundaries."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pluggy
import pytest

from pynchy.agent_home import (
    CompanionSkillAccess,
    parse_skill_tier,
    refresh_personalized_skills,
    sync_skills,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_parse_skill_tier_defaults_when_metadata_cannot_be_read(tmp_path: Path) -> None:
    skill = tmp_path / "unreadable"
    skill.mkdir()
    (skill / "SKILL.md").mkdir()

    assert parse_skill_tier(skill) == ("unreadable", "community")


class _FakePluginManager(pluggy.PluginManager):
    def __init__(self, hook: MagicMock) -> None:
        self.hook = hook


def test_refresh_personalized_skills_skips_trees_containing_symlinks(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    source = project_root / "data/personalization/skills/linked"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: linked\ntier: community\n---\n")
    (source / "outside.txt").symlink_to(tmp_path / "outside.txt")

    session_dir = tmp_path / "session"
    refresh_personalized_skills(
        session_dir,
        project_root=project_root,
        workspace_skills=["*"],
        denied_skill_names=[],
    )

    assert not (session_dir / "skills/linked").exists()


def test_refresh_personalized_skills_prunes_unselected_companion_directories(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    companion = session_dir / "skills/provider"
    companion.mkdir(parents=True)
    (companion / "SKILL.md").write_text("---\nname: provider\ntier: community\n---\n")

    refresh_personalized_skills(
        session_dir,
        project_root=tmp_path / "project",
        workspace_skills=["*"],
        denied_skill_names=[],
        companion_skill_access=CompanionSkillAccess(
            selected_names=frozenset(),
            all_names=frozenset({"provider"}),
        ),
    )

    assert not companion.exists()


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_refresh_personalized_skills_prunes_unselected_companion_non_directories(
    tmp_path: Path, kind: str
) -> None:
    session_dir = tmp_path / "session"
    skills_dir = session_dir / "skills"
    skills_dir.mkdir(parents=True)
    companion = skills_dir / "provider"
    if kind == "file":
        companion.write_text("unexpected skill shape\n")
    else:
        target = tmp_path / "outside"
        target.write_text("unexpected skill shape\n")
        companion.symlink_to(target)

    refresh_personalized_skills(
        session_dir,
        project_root=tmp_path / "project",
        workspace_skills=["*"],
        denied_skill_names=[],
        companion_skill_access=CompanionSkillAccess(
            selected_names=frozenset(),
            all_names=frozenset({"provider"}),
        ),
    )

    assert not companion.exists()
    assert not companion.is_symlink()


def test_sync_skills_rejects_plugin_skill_collision_with_reserved_marker(
    tmp_path: Path,
) -> None:
    plugin_skill = tmp_path / "plugin/skill"
    plugin_skill.mkdir(parents=True)
    (plugin_skill / "SKILL.md").write_text("---\nname: skill\ntier: community\n---\n")

    session_dir = tmp_path / "session"
    destination = session_dir / "skills/skill"
    destination.mkdir(parents=True)
    (destination / ".pynchy-reserved").write_text("managed elsewhere\n")

    hook = MagicMock()
    hook.pynchy_skill_paths.return_value = [[str(plugin_skill)]]

    with pytest.raises(ValueError, match="Skill name collision"):
        sync_skills(
            session_dir,
            project_root=tmp_path / "project",
            plugin_manager=_FakePluginManager(hook),
            workspace_skills=["*"],
        )
