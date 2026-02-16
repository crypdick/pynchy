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

import importlib
import pkgutil
import sys
import tomllib

import pluggy

import pynchy.plugin as plugin_pkg
from pynchy.config import get_settings
from pynchy.logger import logger
from pynchy.plugin.hookspecs import PynchySpec

__all__ = [
    "get_plugin_manager",
]


def _load_managed_plugin_entrypoints(pm: pluggy.PluginManager) -> set[str]:
    """Register plugins from config-managed clones under ~/.config/pynchy/plugins."""
    s = get_settings()
    blocked_entrypoint_names: set[str] = set()

    for plugin_name, plugin_cfg in s.plugins.items():
        if not plugin_cfg.enabled:
            continue
        plugin_root = s.plugins_dir / plugin_name
        pyproject = plugin_root / "pyproject.toml"
        if not pyproject.exists():
            logger.warning(
                "Configured plugin repo is missing pyproject.toml",
                plugin=plugin_name,
                path=str(pyproject),
            )
            continue
        try:
            data = tomllib.loads(pyproject.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            logger.exception("Failed to parse plugin pyproject", plugin=plugin_name)
            continue

        entry_points = data.get("project", {}).get("entry-points", {}).get("pynchy", {})
        if not isinstance(entry_points, dict):
            continue

        src_path = plugin_root / "src"
        if src_path.exists():
            src_path_str = str(src_path)
            if src_path_str not in sys.path:
                sys.path.insert(0, src_path_str)
        else:
            root_str = str(plugin_root)
            if root_str not in sys.path:
                sys.path.insert(0, root_str)

        for entry_name, target in entry_points.items():
            blocked_entrypoint_names.add(str(entry_name))
            if not isinstance(target, str) or ":" not in target:
                logger.warning(
                    "Invalid pynchy entry point in managed plugin",
                    plugin=plugin_name,
                    entry=entry_name,
                    target=target,
                )
                continue
            module_name, attr_name = target.split(":", 1)
            try:
                module = importlib.import_module(module_name)
                plugin_obj = getattr(module, attr_name)
                if isinstance(plugin_obj, type):
                    plugin_obj = plugin_obj()
                pm.register(plugin_obj, name=f"managed-{plugin_name}-{entry_name}")
                logger.info(
                    "Registered managed plugin",
                    plugin=plugin_name,
                    entry=entry_name,
                )
            except Exception:
                logger.exception(
                    "Failed to register managed plugin entry point",
                    plugin=plugin_name,
                    entry=entry_name,
                    target=target,
                )

    return blocked_entrypoint_names


def get_plugin_manager() -> pluggy.PluginManager:
    """Create and configure the plugin manager.

    Discovers plugins from entry points and registers built-in plugins.
    All hook specifications are validated at registration time.

    Returns:
        Configured PluginManager ready to call hooks
    """
    pm = pluggy.PluginManager("pynchy")
    pm.add_hookspecs(PynchySpec)

    # Auto-discover and register all builtin_*.py plugins in this directory.
    # Any file matching builtin_*.py with a class ending in "Plugin" gets registered.
    for _finder, module_name, _is_pkg in pkgutil.iter_modules(
        plugin_pkg.__path__, plugin_pkg.__name__ + "."
    ):
        short = module_name.split(".")[-1]
        if not short.startswith("builtin_"):
            continue
        try:
            mod = importlib.import_module(module_name)
        except Exception:
            logger.exception("Failed to import built-in plugin", module=module_name)
            continue

        for attr_name in dir(mod):
            cls = getattr(mod, attr_name)
            if isinstance(cls, type) and attr_name.endswith("Plugin"):
                plugin_name = short.removeprefix("builtin_")
                pm.register(cls(), name=f"builtin-{plugin_name}")
                logger.info("Registered built-in plugin", name=plugin_name)

    blocked = _load_managed_plugin_entrypoints(pm)
    for entry_name in blocked:
        pm.set_blocked(entry_name)

    # Discover and register third-party plugins from entry points
    # Plugins register via "pynchy" group in their pyproject.toml
    discovered = pm.load_setuptools_entrypoints("pynchy")
    if discovered:
        logger.info("Discovered third-party plugins", count=discovered)

    # Defensive cleanup: some entrypoint loaders can return plugin classes
    # instead of instances, which then fail hook invocation with missing `self`.
    for plugin in list(pm.get_plugins()):
        if isinstance(plugin, type):
            plugin_name = pm.get_name(plugin) or plugin.__name__
            pm.unregister(plugin=plugin)
            logger.warning(
                "Unregistered invalid class-based plugin object",
                plugin=plugin_name,
            )

    # Log plugin summary
    plugin_names = [pm.get_name(p) for p in pm.get_plugins()]
    logger.info("Plugin manager ready", plugins=plugin_names)

    return pm
