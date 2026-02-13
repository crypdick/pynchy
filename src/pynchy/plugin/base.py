"""Plugin system base classes and registry.

This module provides the core infrastructure for plugin discovery and registration.
All plugin types inherit from PluginBase.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field


class PluginBase(ABC):
    """Base class for all pynchy plugins.

    Plugins extend this class and declare their capabilities via class attributes.
    """

    name: str
    version: str = "0.1.0"
    categories: list[str]  # Must be set by plugin
    description: str = ""

    def validate(self) -> None:
        """Validate plugin configuration.

        Called during discovery. Raise ValueError if the plugin is misconfigured.

        Raises:
            ValueError: If plugin is invalid (missing name, no categories, etc.)
        """
        if not getattr(self, "name", None):
            raise ValueError("Plugin must have a name")
        if not getattr(self, "categories", None):
            raise ValueError("Plugin must declare at least one category")

        valid_categories = {"runtime", "channel", "mcp", "skill", "hook"}
        for category in self.categories:
            if category not in valid_categories:
                raise ValueError(
                    f"Invalid category '{category}'. Must be one of: {valid_categories}"
                )


@dataclass
class PluginRegistry:
    """Registry of all discovered plugins.

    Plugins are stored both in the all_plugins list and in category-specific lists.
    Composite plugins appear in multiple category lists.
    """

    all_plugins: list[PluginBase] = field(default_factory=list)
    runtimes: list[PluginBase] = field(default_factory=list)
    channels: list[PluginBase] = field(default_factory=list)
    mcp_servers: list[PluginBase] = field(default_factory=list)
    skills: list[PluginBase] = field(default_factory=list)
    hooks: list[PluginBase] = field(default_factory=list)
