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


def _sync_skills(session_dir: Path, plugin_manager: pluggy.PluginManager | None = None) -> None:
    """Copy container/skills/ and plugin skills into the session's .claude/skills/ directory.

    Args:
        session_dir: Path to the .claude directory for this session
        plugin_manager: Optional pluggy.PluginManager for plugin skills
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
