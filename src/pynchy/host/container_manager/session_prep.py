"""Session directory preparation — skills sync and settings.json.

Prepares per-group agent home directories that get mounted into the container.
"""

from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pluggy

from pynchy.config import get_settings
from pynchy.logger import logger

# ---------------------------------------------------------------------------
# Skill tier helpers
# ---------------------------------------------------------------------------

_DEFAULT_TIER = "community"
_LEARNED_TIER = "learned"
_LEARNED_SKILL_MARKER = ".pynchy-learned-skill"


class _LearnedSkillSyncError(Exception):
    pass


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


def is_skill_selected(name: str, tier: str, workspace_skills: list[str] | None) -> bool:
    """Determine whether a skill should be included for a workspace.

    Resolution rules:
    - ``workspace_skills is None`` → core only (safe default)
    - ``"*"`` in the list → include everything
    - Tier matches an entry → include
    - Name matches an entry → include
    - ``tier == "core"`` → always included when any filtering is active
    """
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


def _sync_skills(
    session_dir: Path,
    plugin_manager: pluggy.PluginManager | None = None,
    *,
    workspace_skills: list[str] | None = None,
    learned_skill_paths: list[Path] | None = None,
) -> None:
    """Copy agent/skills/ and plugin skills into the session's skills directory.

    Args:
        session_dir: Path to the agent home directory for this session
        plugin_manager: Optional pluggy.PluginManager for plugin skills
        workspace_skills: Skill tier/name filter from workspace config; None = core only
        learned_skill_paths: Optional learned skill directories from the Obsidian vault
    """
    s = get_settings()
    skills_dst = session_dir / "skills"
    skills_dst.mkdir(parents=True, exist_ok=True)

    # Copy built-in skills
    skills_src = s.project_root / "src" / "pynchy" / "agent" / "skills"
    if skills_src.exists():
        for skill_dir in skills_src.iterdir():
            if not skill_dir.is_dir():
                continue
            name, tier = parse_skill_tier(skill_dir)
            if not is_skill_selected(name, tier, workspace_skills):
                logger.debug("Skipping skill (not selected)", skill=name, tier=tier)
                continue
            dst_dir = skills_dst / skill_dir.name
            if _is_learned_skill_copy(dst_dir):
                shutil.rmtree(dst_dir)
            _copy_direct_skill_files(skill_dir, dst_dir)

    # Copy plugin skills
    if plugin_manager:
        # Hook returns list of lists (one list per plugin)
        skill_path_lists = plugin_manager.hook.pynchy_skill_paths()
        for skill_paths in skill_path_lists:
            try:
                for skill_path_str in skill_paths:
                    skill_path = Path(skill_path_str)
                    if not skill_path.exists() or not skill_path.is_dir():
                        logger.warning(
                            "Plugin skill path does not exist or is not a directory",
                            path=str(skill_path),
                        )
                        continue

                    name, tier = parse_skill_tier(skill_path)
                    if not is_skill_selected(name, tier, workspace_skills):
                        logger.debug("Skipping plugin skill (not selected)", skill=name, tier=tier)
                        continue

                    dst_dir = skills_dst / skill_path.name
                    if _is_learned_skill_copy(dst_dir):
                        shutil.rmtree(dst_dir)
                    if dst_dir.exists():
                        raise ValueError(
                            f"Skill name collision: skill '{skill_path.name}' conflicts with "
                            f"an existing skill. Rename the plugin skill directory to "
                            f"avoid shadowing built-in or other plugin skills."
                        )

                    shutil.copytree(skill_path, dst_dir)
                    logger.info(
                        "Synced plugin skill",
                        skill=skill_path.name,
                    )
            except ValueError:
                raise  # Re-raise name collisions — these must not be silenced
            except (OSError, TypeError):
                logger.exception("Failed to sync plugin skills")

    desired_learned_skill_names = _selected_learned_skill_names(
        learned_skill_paths,
        workspace_skills,
    )
    _prune_stale_learned_skill_copies(skills_dst, desired_learned_skill_names)

    if learned_skill_paths and workspace_skills is not None:
        _sync_learned_skills(skills_dst, learned_skill_paths, workspace_skills)


def _copy_direct_skill_files(skill_dir: Path, dst_dir: Path) -> None:
    dst_dir.mkdir(parents=True, exist_ok=True)
    for f in skill_dir.iterdir():
        if f.is_file():
            shutil.copy2(f, dst_dir / f.name)


def _is_learned_skill_copy(dst_dir: Path) -> bool:
    try:
        dst_stat = dst_dir.lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(dst_stat.st_mode):
        return False

    marker = dst_dir / _LEARNED_SKILL_MARKER
    try:
        marker_stat = marker.lstat()
    except OSError:
        return False
    return stat.S_ISREG(marker_stat.st_mode)


def _selected_learned_skill_names(
    learned_skill_paths: list[Path] | None,
    workspace_skills: list[str] | None,
) -> set[str]:
    if learned_skill_paths is None or not _learned_skills_selected(workspace_skills):
        return set()

    selected_names: set[str] = set()
    for skill_path in learned_skill_paths:
        if not skill_path.exists() or not skill_path.is_dir():
            continue

        selected_names.add(skill_path.name)

    return selected_names


def _learned_skills_selected(workspace_skills: list[str] | None) -> bool:
    if workspace_skills is None:
        return False
    return _LEARNED_TIER in workspace_skills or "*" in workspace_skills


def _prune_stale_learned_skill_copies(skills_dst: Path, desired_names: set[str]) -> None:
    for dst_dir in sorted(skills_dst.iterdir(), key=lambda path: path.name):
        if dst_dir.name in desired_names:
            continue
        if not _is_learned_skill_copy(dst_dir):
            continue

        try:
            shutil.rmtree(dst_dir)
        except OSError as exc:
            logger.warning(
                "Failed to prune stale learned skill",
                skill=dst_dir.name,
                path=str(dst_dir),
                err=str(exc),
            )


def _sync_learned_skills(
    skills_dst: Path,
    learned_skill_paths: list[Path],
    workspace_skills: list[str],
) -> None:
    # Learned skill collisions are non-fatal because vault content should not
    # be able to break startup.
    for skill_path in learned_skill_paths:
        if not skill_path.exists() or not skill_path.is_dir():
            logger.warning(
                "Skipping learned skill",
                path=str(skill_path),
                reason="not a directory",
            )
            continue

        if not _learned_skills_selected(workspace_skills):
            logger.debug(
                "Skipping learned skill (not selected)",
                skill=skill_path.name,
                tier=_LEARNED_TIER,
            )
            continue

        dst_dir = skills_dst / skill_path.name
        if dst_dir.exists() and not _is_learned_skill_copy(dst_dir):
            logger.warning(
                "Skipping learned skill",
                skill=skill_path.name,
                path=str(skill_path),
                reason="collision",
            )
            continue

        try:
            if dst_dir.exists():
                shutil.rmtree(dst_dir)
            _copy_learned_skill_files(skill_path, dst_dir)
            (dst_dir / _LEARNED_SKILL_MARKER).write_text("managed by pynchy\n")
        except (OSError, _LearnedSkillSyncError) as exc:
            if dst_dir.exists():
                shutil.rmtree(dst_dir, ignore_errors=True)
            logger.warning(
                "Skipping learned skill",
                skill=skill_path.name,
                path=str(skill_path),
                reason="copy failed",
                err=str(exc),
            )
            continue

        logger.info("Synced learned skill", skill=skill_path.name)


def _copy_learned_skill_files(skill_dir: Path, dst_dir: Path) -> None:
    resolved_skill_dir = skill_dir.resolve()
    files = _validated_direct_learned_files(skill_dir, resolved_skill_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy2(f, dst_dir / f.name)


def _validated_direct_learned_files(skill_dir: Path, resolved_skill_dir: Path) -> list[Path]:
    files: list[Path] = []
    for f in sorted(skill_dir.iterdir(), key=lambda path: path.name):
        if f.name == _LEARNED_SKILL_MARKER:
            continue
        if f.is_symlink():
            raise _LearnedSkillSyncError(f"learned skill file is a symlink: {f}")
        if f.is_dir():
            continue
        if not f.is_file():
            continue
        try:
            f.resolve(strict=True).relative_to(resolved_skill_dir)
        except (OSError, ValueError) as exc:
            raise _LearnedSkillSyncError(f"learned skill file escapes skill dir: {f}") from exc
        files.append(f)
    return files


def _write_settings_json(session_dir: Path) -> None:
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
    hook_settings_file = (
        get_settings().project_root / "src" / "pynchy" / "agent" / "scripts" / "settings.json"
    )
    if hook_settings_file.exists():
        try:
            hook_settings = json.loads(hook_settings_file.read_text())
            if "hooks" in hook_settings:
                settings["hooks"] = hook_settings["hooks"]
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to merge hook settings", err=str(exc))

    settings_file.write_text(json.dumps(settings, indent=2) + "\n")
