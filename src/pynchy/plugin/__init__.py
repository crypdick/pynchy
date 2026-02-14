"""Plugin system for pynchy.

Plugins extend pynchy with external capabilities like agent cores,
communication channels, MCP tools, and skills.

Built on pluggy (pytest's plugin framework) for robust, type-safe plugin management.

Usage:
    from pynchy.plugin import get_plugin_manager

    pm = get_plugin_manager()
    cores = pm.hook.pynchy_agent_core_info()  # List of agent core dicts
    channel = pm.hook.pynchy_create_channel(context=ctx)  # First channel that matches
"""

from __future__ import annotations

import pluggy

from pynchy.logger import logger
from pynchy.plugin.hookspecs import PynchySpec

__all__ = [
    "get_plugin_manager",
]


def get_plugin_manager() -> pluggy.PluginManager:
    """Create and configure the plugin manager.

    Discovers plugins from entry points and registers built-in plugins.
    All hook specifications are validated at registration time.

    Returns:
        Configured PluginManager ready to call hooks
    """
    pm = pluggy.PluginManager("pynchy")
    pm.add_hookspecs(PynchySpec)

    # Register built-in plugins directly
    # Built-ins are always available, no installation required
    from pynchy.plugin.builtin_agent_claude import ClaudeAgentCorePlugin

    pm.register(ClaudeAgentCorePlugin(), name="builtin-claude")
    logger.info("Registered built-in plugin", name="claude", category="agent_core")

    # Discover and register third-party plugins from entry points
    # Plugins register via "pynchy" group in their pyproject.toml
    discovered = pm.load_setuptools_entrypoints("pynchy")
    if discovered:
        logger.info("Discovered third-party plugins", count=discovered)

    # Log plugin summary
    plugin_names = [pm.get_name(p) for p in pm.get_plugins()]
    logger.info("Plugin manager ready", plugins=plugin_names)

    return pm
