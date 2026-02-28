"""Session directory preparation — skills sync and settings.json.

Prepares the per-group .claude/ directory that gets mounted into the container.
"""

from __future__ import annotations

import json
import shutil
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


def _parse_skill_tier(skill_dir: Path) -> tuple[str, str]:
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


def _is_skill_selected(name: str, tier: str, workspace_skills: list[str] | None) -> bool:
    """Determine whether a skill should be included for a workspace.

    Resolution rules:
    - ``workspace_skills is None`` → core only (safe default)
    - ``"all"`` in the list → include everything
    - Tier matches an entry → include
    - Name matches an entry → include
    - ``tier == "core"`` → always included when any filtering is active
    """
    if workspace_skills is None:
        return tier == "core"
    if "all" in workspace_skills:
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
) -> None:
    """Copy container/skills/ and plugin skills into the session's .claude/skills/ directory.

    Args:
        session_dir: Path to the .claude directory for this session
        plugin_manager: Optional pluggy.PluginManager for plugin skills
        workspace_skills: Skill tier/name filter from workspace config; None = core only
    """
    s = get_settings()
    skills_dst = session_dir / "skills"
    skills_dst.mkdir(parents=True, exist_ok=True)

    # Copy built-in skills
    skills_src = s.project_root / "container" / "skills"
    if skills_src.exists():
        for skill_dir in skills_src.iterdir():
            if not skill_dir.is_dir():
                continue
            name, tier = _parse_skill_tier(skill_dir)
            if not _is_skill_selected(name, tier, workspace_skills):
                logger.debug("Skipping skill (not selected)", skill=name, tier=tier)
                continue
            dst_dir = skills_dst / skill_dir.name
            dst_dir.mkdir(parents=True, exist_ok=True)
            for f in skill_dir.iterdir():
                if f.is_file():
                    shutil.copy2(f, dst_dir / f.name)

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

                    name, tier = _parse_skill_tier(skill_path)
                    if not _is_skill_selected(name, tier, workspace_skills):
                        logger.debug("Skipping plugin skill (not selected)", skill=name, tier=tier)
                        continue

                    dst_dir = skills_dst / skill_path.name
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

    # Merge hook config from container/scripts/settings.json
    hook_settings_file = get_settings().project_root / "container" / "scripts" / "settings.json"
    if hook_settings_file.exists():
        try:
            hook_settings = json.loads(hook_settings_file.read_text())
            if "hooks" in hook_settings:
                settings["hooks"] = hook_settings["hooks"]
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to merge hook settings", err=str(exc))

    settings_file.write_text(json.dumps(settings, indent=2) + "\n")
