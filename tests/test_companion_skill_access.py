"""Tool grants are the sole authority for companion skill installation."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pluggy
import pytest
from conftest import configure_learning_paths_for, configure_skill_activation_for, make_settings

from pynchy.agent_home import (
    CompanionSkillAccess,
    is_skill_selected,
    sync_skills,
)
from pynchy.config.api import LearningConfig, ProfileConfig, WorkspaceConfig, WorkspaceTool
from pynchy.host.learning.skill_activation import prepare_agent_homes

if TYPE_CHECKING:
    from pathlib import Path


def _write_skill(root: Path, name: str, tier: str = "community") -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(f"---\nname: {name}\ntier: {tier}\n---\n# {name}\n")
    return skill


class _FakeHook:
    def __init__(self, *skill_paths: Path) -> None:
        self._skill_paths = skill_paths

    def pynchy_skill_paths(self) -> list[list[str]]:
        return [[str(path) for path in self._skill_paths]]


class _FakePluginManager(pluggy.PluginManager):
    def __init__(self, *skill_paths: Path) -> None:
        self.hook = _FakeHook(*skill_paths)


def _companion_access(*selected_names: str) -> CompanionSkillAccess:
    return CompanionSkillAccess(
        selected_names=frozenset(selected_names),
        all_names=frozenset({"provider-skill"}),
    )


@pytest.mark.parametrize(
    ("tier", "workspace_skills"),
    [
        ("community", ["*"]),
        ("core", ["core"]),
        ("community", ["provider-skill"]),
        ("learned", ["learned"]),
    ],
)
def test_unselected_companion_precedes_ordinary_skill_selection(
    tier: str,
    workspace_skills: list[str],
) -> None:
    assert not is_skill_selected(
        "provider-skill",
        tier,
        workspace_skills,
        companion_skill_access=_companion_access(),
    )


def test_selected_companion_is_automatic_and_revocation_prunes_warm_copy(
    tmp_path: Path,
) -> None:
    companion = _write_skill(tmp_path / "plugins", "provider-skill")
    ordinary = _write_skill(tmp_path / "plugins", "ordinary-skill")
    plugin_manager = _FakePluginManager(companion, ordinary)
    agent_home = tmp_path / "session/.codex"

    sync_skills(
        agent_home,
        project_root=tmp_path,
        plugin_manager=plugin_manager,
        workspace_skills=[],
        companion_skill_access=_companion_access("provider-skill"),
    )

    assert (agent_home / "skills/provider-skill").is_dir()
    assert not (agent_home / "skills/ordinary-skill").exists()

    sync_skills(
        agent_home,
        project_root=tmp_path,
        plugin_manager=plugin_manager,
        workspace_skills=["*", "provider-skill"],
        companion_skill_access=_companion_access(),
    )

    assert not (agent_home / "skills/provider-skill").exists()
    assert (agent_home / "skills/ordinary-skill").is_dir()


def test_workspace_tool_policy_applies_to_claude_and_codex_plugin_skills(
    tmp_path: Path,
) -> None:
    companion = _write_skill(tmp_path / "plugins", "github-auth")
    ordinary = _write_skill(tmp_path / "plugins", "ordinary-skill")
    plugin_manager = _FakePluginManager(companion, ordinary)
    settings = make_settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        learning=LearningConfig(enabled=False),
        tools={
            "github-cli": WorkspaceTool(
                type="workspace",
                skills=["github-auth"],
            )
        },
        profiles={
            "blocked": ProfileConfig(skills=["*", "github-auth"]),
            "granted": ProfileConfig(tools=["github-cli"]),
        },
        workspaces={
            "blocked": WorkspaceConfig(profiles=["blocked"]),
            "granted": WorkspaceConfig(profiles=["granted"]),
        },
    )
    configure_skill_activation_for(settings)
    configure_learning_paths_for(settings)

    with patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=settings):
        prepare_agent_homes("blocked", plugin_manager)
        prepare_agent_homes("granted", plugin_manager)

    for agent_registry in (".claude", ".codex"):
        blocked = tmp_path / "data/sessions/blocked" / agent_registry / "skills"
        granted = tmp_path / "data/sessions/granted" / agent_registry / "skills"
        assert not (blocked / "github-auth").exists()
        assert (blocked / "ordinary-skill").is_dir()
        assert (granted / "github-auth").is_dir()
        assert not (granted / "ordinary-skill").exists()
