"""Plugin system for pynchy.

Plugins extend pynchy with external capabilities like alternative runtimes,
communication channels, MCP tools, skills, and lifecycle hooks.

Usage:
    from pynchy.plugin import discover_plugins

    registry = discover_plugins()
    # Use registry.runtimes, registry.channels, etc.
"""

from __future__ import annotations

from importlib.metadata import entry_points

from pynchy.logger import logger
from pynchy.plugin.base import PluginBase, PluginRegistry
from pynchy.plugin.channel import ChannelPlugin, PluginContext
from pynchy.plugin.mcp import McpPlugin, McpServerSpec
from pynchy.plugin.skill import SkillPlugin

__all__ = [
    "PluginBase",
    "PluginRegistry",
    "ChannelPlugin",
    "PluginContext",
    "McpPlugin",
    "McpServerSpec",
    "SkillPlugin",
    "discover_plugins",
]


def discover_plugins() -> PluginRegistry:
    """Discover all installed plugins via entry points.

    Scans for plugins registered under the "pynchy.plugins" entry point group.
    Each plugin is instantiated, validated, and registered in the appropriate
    category lists.

    Broken plugins are logged and skipped - they don't crash the application.

    Returns:
        PluginRegistry: Registry containing all discovered plugins
    """
    registry = PluginRegistry()

    for ep in entry_points(group="pynchy.plugins"):
        try:
            # Load and instantiate the plugin class
            plugin_class = ep.load()
            plugin = plugin_class()

            # Validate plugin configuration
            plugin.validate()

            # Register in all_plugins list
            registry.all_plugins.append(plugin)

            # Register in category-specific lists
            # Note: Uses 'if' not 'elif' - composite plugins appear in multiple lists
            if "runtime" in plugin.categories:
                registry.runtimes.append(plugin)
            if "channel" in plugin.categories:
                registry.channels.append(plugin)
            if "mcp" in plugin.categories:
                registry.mcp_servers.append(plugin)
            if "skill" in plugin.categories:
                registry.skills.append(plugin)
            if "hook" in plugin.categories:
                registry.hooks.append(plugin)

            logger.info(
                "Plugin discovered",
                name=plugin.name,
                version=plugin.version,
                categories=plugin.categories,
            )

        except Exception as e:
            logger.warning("Failed to load plugin", name=ep.name, error=str(e))
            # Continue with other plugins - don't crash the app

    logger.info(
        "Plugin discovery complete",
        total=len(registry.all_plugins),
        runtimes=len(registry.runtimes),
        channels=len(registry.channels),
        mcp_servers=len(registry.mcp_servers),
        skills=len(registry.skills),
        hooks=len(registry.hooks),
    )

    return registry
