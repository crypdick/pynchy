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

    ## Security / Trust Model

    **All plugin code runs on the host** during discovery and initialization.
    Entry point loading, ``__init__``, ``validate()``, and category-specific
    methods (``skill_paths()``, ``mcp_server_spec()``, ``create_channel()``)
    all execute in the host Python process with full filesystem and network
    access. Installing a plugin is equivalent to trusting its code.

    The per-category risk profile varies — see each subclass docstring:

    - **Channel plugins** — highest risk: persistent host-process execution.
    - **Skill plugins** — medium risk: brief host execution, content runs in
      container, but ``skill_paths()`` can read arbitrary host paths.
    - **MCP plugins** — lower risk: brief host execution; the MCP server itself
      runs inside the container sandbox with read-only source mounts.
    - **Hook plugins** — medium risk: class runs on host, hook code runs inside
      the container via dynamic import, but the module path is host-controlled.
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

        valid_categories = {"runtime", "channel", "mcp", "skill", "hook", "agent_core"}
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
    agent_cores: list[PluginBase] = field(default_factory=list)
