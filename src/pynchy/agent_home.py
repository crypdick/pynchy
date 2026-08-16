"""Session directory preparation — skills sync and settings.json.

Prepares per-group agent home directories that get mounted into the container.
"""

from __future__ import annotations

import json
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pluggy

from pynchy.logger import logger

# ---------------------------------------------------------------------------
# Skill tier helpers
# ---------------------------------------------------------------------------

_DEFAULT_TIER = "community"
_PLUGIN_SKILL_MARKER = ".pynchy-plugin-skill"
_PERSONALIZED_SKILL_MARKER = ".pynchy-personalized-skill"
_SKILL_NAME_COLLISION_ERROR = (
    "Skill name collision: skill '{skill_name}' conflicts with an existing skill. "
    "Rename the plugin skill directory to avoid shadowing a default or other plugin skill."
)


@dataclass(frozen=True, slots=True)
class CompanionSkillAccess:
    """Companion names owned by all tools and by this workspace's available tools."""

    selected_names: frozenset[str]
    all_names: frozenset[str]


_NO_COMPANION_SKILL_ACCESS = CompanionSkillAccess(frozenset(), frozenset())


def parse_skill_tier(skill_dir: Path) -> tuple[str, str]:
    """Read ``name`` and ``tier`` from a skill's SKILL.md YAML frontmatter.

    Uses simple line-based parsing (no PyYAML dependency). Returns
    ``(name, tier)`` where *name* defaults to the directory name and *tier*
    defaults to ``"community"`` when the field is absent.
    """
    name = skill_dir.name
    tier = _DEFAULT_TIER

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return name, tier

    try:
        lines = skill_md.read_text().splitlines()
    except OSError:
        return name, tier

    if not lines or lines[0].strip() != "---":
        return name, tier

    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if stripped.startswith("name:"):
            name = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("tier:"):
            tier = stripped.split(":", 1)[1].strip()

    return name, tier


def is_skill_selected(
    name: str,
    tier: str,
    workspace_skills: list[str] | None,
    *,
    companion_skill_access: CompanionSkillAccess = _NO_COMPANION_SKILL_ACCESS,
) -> bool:
    """Determine whether a skill should be included for a workspace.

    Resolution rules:
    - Tool companion names require an available owning tool
    - ``workspace_skills is None`` → core only (safe default)
    - ``"*"`` in the list → include everything
    - Tier matches an entry → include
    - Name matches an entry → include
    - ``tier == "core"`` → always included when any filtering is active
    """
    if name in companion_skill_access.all_names:
        return name in companion_skill_access.selected_names
    if workspace_skills is None:
        return tier == "core"
    if "*" in workspace_skills:
        return True
    if tier in workspace_skills:
        return True
    if name in workspace_skills:
        return True
    return tier == "core"


# ---------------------------------------------------------------------------
# Skill sync
# ---------------------------------------------------------------------------


def sync_skills(  # noqa: PLR0913 - source selection and companion authorization are independent inputs.
    session_dir: Path,
    *,
    project_root: Path,
    plugin_manager: pluggy.PluginManager | None = None,
    workspace_skills: list[str] | None = None,
    denied_skill_names: list[str] | None = None,
    companion_skill_access: CompanionSkillAccess = _NO_COMPANION_SKILL_ACCESS,
) -> None:
    """Copy selected canonical skills into one generated agent registry.

    Args:
        session_dir: Path to the agent home directory for this session
        plugin_manager: Optional pluggy.PluginManager for plugin skills
        workspace_skills: Skill tier/name filter from workspace config; None = core only
    """
    skills_dst = session_dir / "skills"
    skills_dst.mkdir(parents=True, exist_ok=True)

    _sync_configured_skills(
        project_root / "data" / "defaults" / "skills",
        skills_dst,
        workspace_skills,
        companion_skill_access,
    )
    refresh_personalized_skills(
        session_dir,
        project_root=project_root,
        workspace_skills=workspace_skills,
        denied_skill_names=denied_skill_names,
        companion_skill_access=companion_skill_access,
    )
    # Tool-associated skills cross this boundary through their owning plugin,
    # keeping built-in and third-party plugins equally pluggable.
    _sync_plugin_skills(
        skills_dst,
        plugin_manager,
        workspace_skills,
        project_root,
        companion_skill_access,
    )


def refresh_personalized_skills(
    session_dir: Path,
    *,
    project_root: Path,
    workspace_skills: list[str] | None,
    denied_skill_names: list[str] | None,
    companion_skill_access: CompanionSkillAccess = _NO_COMPANION_SKILL_ACCESS,
) -> None:
    """Refresh canonical personalization skills in an existing agent home."""
    skills_dst = session_dir / "skills"
    skills_dst.mkdir(parents=True, exist_ok=True)
    _prune_unauthorized_companion_skills(
        skills_dst,
        companion_skill_access,
    )
    skills_src = project_root / "data" / "personalization" / "skills"
    desired_names = _selected_personalized_skill_names(
        skills_src,
        workspace_skills,
        denied_skill_names,
        companion_skill_access,
    )
    _prune_stale_personalized_skill_copies(skills_dst, desired_names)
    _sync_personalized_skills(
        skills_src,
        skills_dst,
        workspace_skills,
        denied_skill_names or [],
        companion_skill_access,
    )


def _prune_unauthorized_companion_skills(
    skills_dst: Path,
    companion_skill_access: CompanionSkillAccess,
) -> None:
    unauthorized = companion_skill_access.all_names - companion_skill_access.selected_names
    if not unauthorized:
        return

    for skill_path in skills_dst.iterdir():
        name = skill_path.name
        if skill_path.is_dir() and not skill_path.is_symlink():
            name, _tier = parse_skill_tier(skill_path)
        if name not in unauthorized:
            continue
        if skill_path.is_symlink() or not skill_path.is_dir():
            skill_path.unlink()
        else:
            shutil.rmtree(skill_path)
        logger.info("Pruned unauthorized tool companion skill", skill=name)


def _sync_configured_skills(
    skills_src: Path,
    skills_dst: Path,
    workspace_skills: list[str] | None,
    companion_skill_access: CompanionSkillAccess,
) -> None:
    if not skills_src.exists():
        return

    for skill_dir in skills_src.iterdir():
        if not skill_dir.is_dir():
            continue
        _sync_configured_skill_dir(
            skill_dir,
            skills_dst,
            workspace_skills,
            companion_skill_access,
        )


def _sync_configured_skill_dir(
    skill_dir: Path,
    skills_dst: Path,
    workspace_skills: list[str] | None,
    companion_skill_access: CompanionSkillAccess,
) -> None:
    name, tier = parse_skill_tier(skill_dir)
    if not is_skill_selected(
        name,
        tier,
        workspace_skills,
        companion_skill_access=companion_skill_access,
    ):
        logger.debug("Skipping skill (not selected)", skill=name, tier=tier)
        return

    dst_dir = skills_dst / skill_dir.name
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    shutil.copytree(skill_dir, dst_dir)


def _sync_plugin_skills(
    skills_dst: Path,
    plugin_manager: pluggy.PluginManager | None,
    workspace_skills: list[str] | None,
    project_root: Path,
    companion_skill_access: CompanionSkillAccess,
) -> None:
    if plugin_manager is None:
        return

    for skill_paths in plugin_manager.hook.pynchy_skill_paths():
        _sync_plugin_skill_paths(
            skills_dst,
            skill_paths,
            workspace_skills,
            project_root,
            companion_skill_access,
        )


def _sync_plugin_skill_paths(
    skills_dst: Path,
    skill_paths: list[Any],
    workspace_skills: list[str] | None,
    project_root: Path,
    companion_skill_access: CompanionSkillAccess,
) -> None:
    for skill_path_str in skill_paths:
        _sync_plugin_skill_path(
            skills_dst,
            skill_path_str,
            workspace_skills,
            project_root,
            companion_skill_access,
        )


def _sync_plugin_skill_path(
    skills_dst: Path,
    skill_path_str: object,
    workspace_skills: list[str] | None,
    project_root: Path,
    companion_skill_access: CompanionSkillAccess,
) -> None:
    if not isinstance(skill_path_str, str | Path):
        logger.exception("Failed to sync plugin skill", path=repr(skill_path_str))
        return
    skill_path = Path(skill_path_str)

    if not skill_path.exists() or not skill_path.is_dir():
        logger.warning(
            "Plugin skill path does not exist or is not a directory",
            path=str(skill_path),
        )
        return

    name, tier = parse_skill_tier(skill_path)
    if not is_skill_selected(
        name,
        tier,
        workspace_skills,
        companion_skill_access=companion_skill_access,
    ):
        logger.debug("Skipping plugin skill (not selected)", skill=name, tier=tier)
        return

    try:
        _copy_plugin_skill_path(skill_path, skills_dst, project_root)
    except (OSError, TypeError):
        logger.exception("Failed to sync plugin skill", path=repr(skill_path_str))


def _copy_plugin_skill_path(skill_path: Path, skills_dst: Path, project_root: Path) -> None:
    dst_dir = skills_dst / skill_path.name
    if _is_marked_skill_copy(dst_dir, _PERSONALIZED_SKILL_MARKER):
        logger.info("Personalized skill overrides plugin skill", skill=skill_path.name)
        return
    if _is_plugin_skill_copy_from(dst_dir, skill_path) or _is_unmarked_plugin_skill_copy(
        dst_dir, skill_path, project_root
    ):
        shutil.rmtree(dst_dir)
    if dst_dir.exists():
        raise ValueError(_SKILL_NAME_COLLISION_ERROR.format(skill_name=skill_path.name))

    shutil.copytree(skill_path, dst_dir)
    (dst_dir / _PLUGIN_SKILL_MARKER).write_text(f"{skill_path.resolve()}\n")
    logger.info("Synced plugin skill", skill=skill_path.name)


def _is_marked_skill_copy(dst_dir: Path, marker_name: str) -> bool:
    try:
        dst_stat = dst_dir.lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(dst_stat.st_mode):
        return False

    marker = dst_dir / marker_name
    try:
        marker_stat = marker.lstat()
    except OSError:
        return False
    return stat.S_ISREG(marker_stat.st_mode)


def _is_plugin_skill_copy_from(dst_dir: Path, skill_path: Path) -> bool:
    try:
        dst_stat = dst_dir.lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(dst_stat.st_mode):
        return False

    marker = dst_dir / _PLUGIN_SKILL_MARKER
    try:
        marker_stat = marker.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(marker_stat.st_mode):
        return False

    try:
        marked_source = marker.read_text().strip()
    except OSError:
        return False
    current_source = str(skill_path.resolve())
    marked_relative = _site_packages_relative(marked_source)
    return marked_source == current_source or (
        marked_relative is not None and marked_relative == _site_packages_relative(current_source)
    )


def _site_packages_relative(source: str) -> tuple[str, ...] | None:
    parts = Path(source).parts
    try:
        index = parts.index("site-packages")
    except ValueError:
        return None
    relative = parts[index + 1 :]
    return relative or None


def _is_unmarked_plugin_skill_copy(dst_dir: Path, skill_path: Path, project_root: Path) -> bool:
    if not dst_dir.exists() or not dst_dir.is_dir():
        return False
    if (dst_dir / _PLUGIN_SKILL_MARKER).exists():
        return False
    if any(path.name.startswith(".pynchy-") for path in dst_dir.iterdir()):
        return False
    configured_sources = (
        project_root / "data/defaults/skills" / skill_path.name,
        project_root / "data/personalization/skills" / skill_path.name,
    )
    return not any(path.exists() for path in configured_sources)


def _selected_personalized_skill_names(
    skills_src: Path,
    workspace_skills: list[str] | None,
    denied_skill_names: list[str] | None,
    companion_skill_access: CompanionSkillAccess,
) -> set[str]:
    if not skills_src.is_dir():
        return set()

    selected_names: set[str] = set()
    denied = set(denied_skill_names or [])
    for skill_path in skills_src.iterdir():
        if (
            not skill_path.exists()
            or not skill_path.is_dir()
            or _skill_tree_contains_symlink(skill_path)
        ):
            continue

        name, tier = parse_skill_tier(skill_path)
        if name not in denied and is_skill_selected(
            name,
            tier,
            workspace_skills,
            companion_skill_access=companion_skill_access,
        ):
            selected_names.add(skill_path.name)

    return selected_names


def _prune_stale_personalized_skill_copies(skills_dst: Path, desired_names: set[str]) -> None:
    for dst_dir in sorted(skills_dst.iterdir(), key=lambda path: path.name):
        personalized = _is_marked_skill_copy(dst_dir, _PERSONALIZED_SKILL_MARKER)
        if personalized and dst_dir.name in desired_names:
            continue
        if not personalized:
            continue

        try:
            shutil.rmtree(dst_dir)
        except OSError as exc:
            logger.warning(
                "Failed to prune stale personalized skill",
                skill=dst_dir.name,
                path=str(dst_dir),
                err=str(exc),
            )


def _sync_personalized_skills(
    skills_src: Path,
    skills_dst: Path,
    workspace_skills: list[str] | None,
    denied_skill_names: list[str],
    companion_skill_access: CompanionSkillAccess,
) -> None:
    if not skills_src.is_dir():
        return
    for skill_path in skills_src.iterdir():
        contains_symlink = _skill_tree_contains_symlink(skill_path)
        if not skill_path.is_dir() or contains_symlink:
            if contains_symlink:
                logger.warning(
                    "Skipping personalized skill containing symlinks",
                    path=str(skill_path),
                )
            continue
        name, tier = parse_skill_tier(skill_path)
        if name in denied_skill_names or not is_skill_selected(
            name,
            tier,
            workspace_skills,
            companion_skill_access=companion_skill_access,
        ):
            logger.debug(
                "Skipping personalized skill (not selected)",
                skill=name,
                tier=tier,
            )
            continue

        dst_dir = skills_dst / skill_path.name
        if dst_dir.is_symlink():
            raise ValueError(_SKILL_NAME_COLLISION_ERROR.format(skill_name=skill_path.name))
        if dst_dir.exists():
            shutil.rmtree(dst_dir)
        shutil.copytree(skill_path, dst_dir)
        (dst_dir / _PERSONALIZED_SKILL_MARKER).write_text("managed by pynchy\n")
        logger.info("Synced personalized skill", skill=skill_path.name)


def _skill_tree_contains_symlink(skill_path: Path) -> bool:
    return skill_path.is_symlink() or any(path.is_symlink() for path in skill_path.rglob("*"))


def write_settings_json(session_dir: Path, *, project_root: Path) -> None:
    """Write Claude Code settings.json, merging hook config from scripts/.

    Always regenerates to pick up hook config changes (e.g. guard_git).
    """
    settings_file = session_dir / "settings.json"
    settings: dict[str, Any] = {
        "env": {
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
            "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0",
            "BASH_MAX_OUTPUT_LENGTH": "90000",
            "MAX_MCP_OUTPUT_TOKENS": "75000",
        },
    }

    # Merge hook config from agent/scripts/settings.json
    hook_settings_file = project_root / "src" / "pynchy" / "agent" / "scripts" / "settings.json"
    if hook_settings_file.exists():
        try:
            hook_settings = json.loads(hook_settings_file.read_text())
            if "hooks" in hook_settings:
                settings["hooks"] = hook_settings["hooks"]
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to merge hook settings", err=str(exc))

    settings_file.write_text(json.dumps(settings, indent=2) + "\n")
