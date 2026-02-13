"""Skill plugin system for agent capabilities.

Enables agent skills (instructions/capabilities) to be provided by external plugins.
Skills define what the agent can do and how to do it.
"""

from __future__ import annotations

from abc import abstractmethod
from pathlib import Path

from pynchy.plugin.base import PluginBase


class SkillPlugin(PluginBase):
    """Base class for skill plugins.

    Skill plugins provide agent skills by exposing directories containing
    SKILL.md files and supporting resources. These skills are synced to
    the agent's session directory before the agent starts.
    """

    categories = ["skill"]  # Fixed category for all skill plugins

    @abstractmethod
    def skill_paths(self) -> list[Path]:
        """Return paths to skill directories.

        Each directory should contain:
        - SKILL.md (skill definition following Claude Agent SDK format)
        - Optional supporting files (examples, templates, etc.)

        The directory structure is copied to the agent's session directory
        and becomes discoverable by the Claude Agent SDK.

        Returns:
            List of Path objects pointing to skill directories
        """
        ...
