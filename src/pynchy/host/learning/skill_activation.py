"""Prepare the per-workspace agent homes that expose learned skills."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path  # noqa: TC003, RUF100 - beartype resolves this runtime annotation.
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pluggy

from pynchy.config import get_settings
from pynchy.host.container_manager.session_prep import (
    refresh_learned_skills,
    sync_skills,
    write_settings_json,
)
from pynchy.host.learning.paths import LearningConfigError, LearningPaths, resolve_learning_paths
from pynchy.host.learning.skills import iter_learned_skill_dirs
from pynchy.host.orchestrator.workspace_config import load_resolved_config

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


def prepare_agent_homes(
    group_folder: str,
    plugin_manager: pluggy.PluginManager | None = None,
) -> PreparedAgentHomes:
    """Sync selected and learned skills into the workspace's agent homes.

    Both cold starts and warm follow-ups use the same bind-mounted homes. Refreshing
    them before each turn makes skills written by the parallel reviewer available on
    the next turn without restarting the session.
    """
    settings = get_settings()
    workspace_skills, denied_skill_names = _workspace_skill_policy(group_folder)
    learning_paths = resolve_learning_paths(
        group_folder,
        profile_override=_learning_profile_override_for_group(group_folder),
    )
    learned_skill_paths: list[Path] | None = None
    if learning_paths is not None:
        _validate_learning_vault(learning_paths.vault_root)
        learning_paths.memory_root.mkdir(parents=True, exist_ok=True)
        # Learned skills stay global so profiles can deliberately share them.
        # The resolved `skills` list is the profile's allowlist; do not add a
        # learned tier implicitly. Keep docs/usage/memory.md aligned.
        learned_skill_paths = iter_learned_skill_dirs(group_folder)

    session_root = settings.data_dir / "sessions" / group_folder
    claude_home = session_root / ".claude"
    claude_home.mkdir(parents=True, exist_ok=True)
    write_settings_json(claude_home)
    sync_skills(
        claude_home,
        plugin_manager,
        workspace_skills=workspace_skills,
        denied_skill_names=denied_skill_names,
        learned_skill_paths=learned_skill_paths,
    )

    codex_home = session_root / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    sync_skills(
        codex_home,
        plugin_manager,
        workspace_skills=workspace_skills,
        denied_skill_names=denied_skill_names,
        learned_skill_paths=learned_skill_paths,
    )
    return PreparedAgentHomes(
        claude_home=claude_home,
        codex_home=codex_home,
        learning_paths=learning_paths,
    )


def refresh_learned_agent_skills(group_folder: str) -> None:
    """Expose skills the parallel reviewer wrote since the last agent turn."""
    learning_paths = resolve_learning_paths(
        group_folder,
        profile_override=_learning_profile_override_for_group(group_folder),
    )
    if learning_paths is None:
        return

    _validate_learning_vault(learning_paths.vault_root)
    workspace_skills, denied_skill_names = _workspace_skill_policy(group_folder)
    learned_skill_paths = iter_learned_skill_dirs(group_folder)
    session_root = get_settings().data_dir / "sessions" / group_folder
    for agent_home in (session_root / ".claude", session_root / ".codex"):
        refresh_learned_skills(
            agent_home,
            workspace_skills=workspace_skills,
            denied_skill_names=denied_skill_names,
            learned_skill_paths=learned_skill_paths,
        )


def _validate_learning_vault(vault_root: Path) -> None:
    if vault_root.exists() and vault_root.is_dir():
        return
    raise LearningConfigError(_LEARNING_VAULT_DIRECTORY_REQUIRED_ERROR)


def _workspace_skill_policy(group_folder: str) -> tuple[list[str] | None, list[str] | None]:
    resolved = load_resolved_config(group_folder)
    if resolved is None:
        return None, None
    return resolved.skills, resolved.denied_skills


def _learning_profile_override_for_group(group_folder: str) -> str | None:
    if not group_folder.startswith(_LEARNING_REVIEW_FOLDER_PREFIX):
        return None
    profile_slug = group_folder.removeprefix(_LEARNING_REVIEW_FOLDER_PREFIX)
    return profile_slug or None
