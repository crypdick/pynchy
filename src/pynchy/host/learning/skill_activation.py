"""Prepare per-workspace agent homes from canonical skill sources."""

from __future__ import annotations

from collections.abc import (
    Callable,
    Mapping,
)
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pluggy

from pynchy.agent_home import (
    CompanionSkillAccess,
    refresh_personalized_skills,
    sync_skills,
    write_settings_json,
)
from pynchy.host.learning.paths import LearningConfigError, LearningPaths

_LEARNING_VAULT_DIRECTORY_REQUIRED_ERROR = (
    "learning.obsidian.vault_root must be an existing directory"
)
_LEARNING_REVIEW_FOLDER_PREFIX = "learning-review-"


@dataclass(frozen=True)
class PreparedAgentHomes:
    """Agent homes and learning context prepared for one workspace."""

    claude_home: Path
    codex_home: Path
    learning_paths: LearningPaths | None


@dataclass(frozen=True)
class _WorkspaceSkillPolicy:
    workspace_skills: list[str] | None
    denied_skill_names: list[str] | None
    companion_skill_access: CompanionSkillAccess


type WorkspaceSkillSelection = tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]


@dataclass(frozen=True)
class SkillActivationRuntime:
    """Resolved skill policy and filesystem roots selected at composition."""

    project_root: Path
    sessions_root: Path
    tool_skills: Mapping[str, tuple[str, ...]]
    resolve_workspace_skill_selection: Callable[[str], WorkspaceSkillSelection | None]
    resolve_learning_paths: Callable[[str, str | None], LearningPaths | None]


_runtime: SkillActivationRuntime | None = None


def configure_skill_activation_runtime(runtime: SkillActivationRuntime) -> None:
    """Inject resolved workspace policy and paths at host composition."""
    global _runtime  # noqa: PLW0603 - one host process owns one skill runtime.
    _runtime = runtime


def _configured_runtime() -> SkillActivationRuntime:
    if _runtime is None:
        raise RuntimeError("skill activation runtime has not been configured")
    return _runtime


def prepare_agent_homes(
    group_folder: str,
    plugin_manager: pluggy.PluginManager | None = None,
) -> PreparedAgentHomes:
    """Sync selected canonical skills into the workspace's agent homes.

    Both cold starts and warm follow-ups use the same bind-mounted homes. Refreshing
    personalized skills before each turn makes reviewer updates available on
    the next turn without restarting the session.
    """
    runtime = _configured_runtime()
    skill_policy = _workspace_skill_policy(group_folder)
    learning_paths = runtime.resolve_learning_paths(
        group_folder, _learning_profile_override_for_group(group_folder)
    )
    if learning_paths is not None:
        _validate_learning_vault(learning_paths.vault_root)
        learning_paths.memory_root.mkdir(parents=True, exist_ok=True)

    personalization_skills = runtime.project_root / "data" / "personalization" / "skills"
    personalization_skills.mkdir(parents=True, exist_ok=True)

    session_root = runtime.sessions_root / group_folder
    claude_home = session_root / ".claude"
    claude_home.mkdir(parents=True, exist_ok=True)
    write_settings_json(claude_home, project_root=runtime.project_root)
    sync_skills(
        claude_home,
        project_root=runtime.project_root,
        plugin_manager=plugin_manager,
        workspace_skills=skill_policy.workspace_skills,
        denied_skill_names=skill_policy.denied_skill_names,
        companion_skill_access=skill_policy.companion_skill_access,
    )

    codex_home = session_root / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    sync_skills(
        codex_home,
        project_root=runtime.project_root,
        plugin_manager=plugin_manager,
        workspace_skills=skill_policy.workspace_skills,
        denied_skill_names=skill_policy.denied_skill_names,
        companion_skill_access=skill_policy.companion_skill_access,
    )
    return PreparedAgentHomes(
        claude_home=claude_home,
        codex_home=codex_home,
        learning_paths=learning_paths,
    )


def refresh_personalized_agent_skills(group_folder: str) -> None:
    """Expose personalization skill updates on the next agent turn."""
    skill_policy = _workspace_skill_policy(group_folder)
    runtime = _configured_runtime()
    session_root = runtime.sessions_root / group_folder
    for agent_home in (session_root / ".claude", session_root / ".codex"):
        refresh_personalized_skills(
            agent_home,
            project_root=runtime.project_root,
            workspace_skills=skill_policy.workspace_skills,
            denied_skill_names=skill_policy.denied_skill_names,
            companion_skill_access=skill_policy.companion_skill_access,
        )


def _validate_learning_vault(vault_root: Path) -> None:
    if vault_root.exists() and vault_root.is_dir():
        return
    raise LearningConfigError(_LEARNING_VAULT_DIRECTORY_REQUIRED_ERROR)


def _workspace_skill_policy(group_folder: str) -> _WorkspaceSkillPolicy:
    runtime = _configured_runtime()
    all_companion_skill_names = frozenset(
        skill_name for skills in runtime.tool_skills.values() for skill_name in skills
    )
    selection = runtime.resolve_workspace_skill_selection(group_folder)
    if selection is None:
        return _WorkspaceSkillPolicy(
            workspace_skills=None,
            denied_skill_names=None,
            companion_skill_access=CompanionSkillAccess(
                selected_names=frozenset(),
                all_names=all_companion_skill_names,
            ),
        )
    workspace_skills, denied_skill_names, selected_tool_names = selection
    selected_companion_skill_names = frozenset(
        skill_name
        for tool_name in selected_tool_names
        for skill_name in runtime.tool_skills.get(tool_name, ())
    )
    return _WorkspaceSkillPolicy(
        workspace_skills=list(workspace_skills),
        denied_skill_names=list(denied_skill_names),
        companion_skill_access=CompanionSkillAccess(
            selected_names=selected_companion_skill_names,
            all_names=all_companion_skill_names,
        ),
    )


def _learning_profile_override_for_group(group_folder: str) -> str | None:
    if not group_folder.startswith(_LEARNING_REVIEW_FOLDER_PREFIX):
        return None
    profile_slug = group_folder.removeprefix(_LEARNING_REVIEW_FOLDER_PREFIX)
    return profile_slug or None
