"""Periodic agent configuration — YAML schema and loader.

Periodic agents are background agents that run on cron schedules.
Each one lives in a groups/{name}/ folder with a periodic.yaml file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from croniter import croniter

from pynchy.config import GROUPS_DIR


@dataclass
class PeriodicAgentConfig:
    schedule: str  # cron expression (required)
    prompt: str  # what to tell the agent each run (required)
    context_mode: Literal["group", "isolated"] = "group"  # resume session or fresh
    project_access: bool = False  # mount host project into container


def load_periodic_config(group_folder: str) -> PeriodicAgentConfig | None:
    """Read groups/{folder}/periodic.yaml, return None if not present."""
    path = GROUPS_DIR / group_folder / "periodic.yaml"
    if not path.exists():
        return None

    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        return None

    schedule = raw.get("schedule")
    prompt = raw.get("prompt")
    if not schedule or not prompt:
        return None

    if not croniter.is_valid(str(schedule)):
        return None

    context_mode = raw.get("context_mode", "group")
    if context_mode not in ("group", "isolated"):
        context_mode = "group"

    project_access = bool(raw.get("project_access", False))

    return PeriodicAgentConfig(
        schedule=str(schedule),
        prompt=str(prompt),
        context_mode=context_mode,
        project_access=project_access,
    )


def write_periodic_config(group_folder: str, config: PeriodicAgentConfig) -> Path:
    """Write a periodic.yaml file for a group. Returns the path written."""
    path = GROUPS_DIR / group_folder / "periodic.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "schedule": config.schedule,
        "prompt": config.prompt,
        "context_mode": config.context_mode,
    }
    if config.project_access:
        data["project_access"] = True
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    return path
