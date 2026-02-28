"""Plugin system for pynchy.

Plugins extend pynchy with external capabilities like agent cores,
communication channels, MCP tools, skills, and managed workspaces.

Built on pluggy (pytest's plugin framework) for robust, type-safe plugin management.

Usage:
    from pynchy.plugins import get_plugin_manager

    pm = get_plugin_manager()
    cores = pm.hook.pynchy_agent_core_info()  # List of agent core dicts
    channels = pm.hook.pynchy_create_channel(context=ctx)  # All matching channels
"""

from __future__ import annotations

import asyncio
import importlib
import warnings
from collections.abc import Callable
from typing import Any

import pluggy

from pynchy.config import get_settings
from pynchy.logger import logger
from pynchy.plugins.hookspecs import PynchySpec

__all__ = [
    "collect_hook_results",
    "get_plugin_manager",
]

# Static registry of built-in plugins.
# Each entry: (module_path, class_name, config_key)
# config_key is checked against [plugins.<key>].enabled in config.toml.
_BUILTIN_PLUGIN_SPECS: list[tuple[str, str, str]] = [
    ("pynchy.agent_framework.plugins.claude", "ClaudeAgentCorePlugin", "claude"),
    ("pynchy.agent_framework.plugins.openai", "OpenAIAgentCorePlugin", "openai"),
    ("pynchy.plugins.channels.slack", "SlackChannelPlugin", "slack"),
    ("pynchy.plugins.channels.tui", "TuiChannelPlugin", "tui"),
    ("pynchy.plugins.channels.whatsapp", "WhatsAppPlugin", "whatsapp"),
    ("pynchy.tunnels.plugins.tailscale", "TailscaleTunnelPlugin", "tailscale"),
    ("pynchy.runtime.plugins.docker_runtime", "DockerRuntimePlugin", "docker-runtime"),
    ("pynchy.runtime.plugins.apple_runtime", "AppleRuntimePlugin", "apple-runtime"),
    ("pynchy.integrations.plugins.caldav", "CalDAVMcpServerPlugin", "caldav"),
    (
        "pynchy.integrations.plugins.slack_token_extractor",
        "SlackTokenExtractorPlugin",
        "slack-token-extractor",
    ),
    (
        "pynchy.integrations.plugins.x_integration",
        "XIntegrationPlugin",
        "x-integration",
    ),
    (
        "pynchy.integrations.plugins.google_setup",
        "GoogleMcpPlugin",
        "google",
    ),
    (
        "pynchy.integrations.plugins.google_setup",
        "GoogleSetupPlugin",
        "google-setup",
    ),
    (
        "pynchy.integrations.plugins.playwright_browser",
        "PlaywrightBrowserPlugin",
        "playwright-browser",
    ),
    (
        "pynchy.integrations.plugins.notebook_server",
        "NotebookServerPlugin",
        "notebook",
    ),
    ("pynchy.observers.plugins.sqlite_observer", "SqliteObserverPlugin", "sqlite-observer"),
    ("pynchy.memory.plugins.sqlite_memory", "SqliteMemoryPlugin", "sqlite-memory"),
]


def get_plugin_manager() -> pluggy.PluginManager:
    """Create and configure the plugin manager.

    Discovers plugins from the static registry and entry points.
    All hook specifications are validated at registration time.

    Returns:
        Configured PluginManager ready to call hooks
    """
    pm = pluggy.PluginManager("pynchy")
    pm.add_hookspecs(PynchySpec)

    s = get_settings()

    # Some plugins (e.g. neonize, used by the WhatsApp channel) call
    # asyncio.get_event_loop() at import time.  Ensure a loop exists so the
    # import succeeds even when called from a sync context or a pytest-xdist
    # worker thread that hasn't set one up yet.  The temp loop wraps both
    # built-in registration (which triggers imports) and entrypoint discovery.
    #
    # We always create and set our own temp loop (when no loop is running) so
    # that any import-time get_event_loop() calls return it instead of
    # auto-creating orphan loops that never get closed.
    #
    # Known neonize bug: neonize/aioze/events.py unconditionally creates a
    # loop at import time via `event_global_loop = asyncio.new_event_loop()`.
    # This loop is used internally for its Go→Python FFI bridge and is never
    # closed.  It causes a ResourceWarning under `pytest -W all` but is
    # harmless at runtime and cannot be fixed on our side without patching
    # neonize.  The temp loop below does NOT address that loop — it only
    # prevents the *second* orphan from neonize/aioze/client.py line 203
    # (`loop = get_event_loop()`), which in Python 3.12+ would auto-create
    # yet another loop if no default is set.
    _tmp_loop = None
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        _tmp_loop = asyncio.new_event_loop()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            asyncio.set_event_loop(_tmp_loop)

    # Register built-in plugins from the static registry.
    for module_path, class_name, config_key in _BUILTIN_PLUGIN_SPECS:
        # Check if plugin is disabled via config.toml [plugins.<key>]
        plugin_cfg = s.plugins.get(config_key)
        if plugin_cfg is not None and not plugin_cfg.enabled:
            logger.info("Plugin disabled via config", plugin=config_key)
            continue

        try:
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
            pm.register(cls(), name=f"builtin-{config_key}")
            logger.info("Registered built-in plugin", name=config_key)
        except ImportError:
            # Graceful skip for plugins with optional deps (whatsapp, slack, caldav)
            logger.debug("Plugin skipped (optional dependency missing)", plugin=config_key)
        except Exception:
            logger.exception("Failed to load built-in plugin", plugin=config_key)

    # Discover and register third-party plugins from entry points
    # Plugins register via "pynchy" group in their pyproject.toml
    discovered = pm.load_setuptools_entrypoints("pynchy")

    # Close the temporary loop now that imports are done.  Leaving it open
    # leaks a ResourceWarning and can block interpreter shutdown.
    if _tmp_loop is not None:
        _tmp_loop.close()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            asyncio.set_event_loop(None)
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


def collect_hook_results(
    hook_attr: str,
    validator: Callable[[Any], bool],
    label: str,
    *,
    pm: pluggy.PluginManager | None = None,
) -> list[Any]:
    """Call a pluggy hook and return validated results.

    Handles plugin manager retrieval, hook invocation, and filtering
    through a validator function.  Invalid or ``None`` results are
    logged and skipped.

    Args:
        hook_attr: Name of the hook attribute on ``pm.hook`` (e.g. ``"pynchy_memory"``).
        validator: Callable that returns ``True`` for valid hook results.
        label: Human-readable label for log messages (e.g. ``"memory"``).
        pm: Optional pre-existing plugin manager.  If ``None``, calls
            :func:`get_plugin_manager`.
    """
    if pm is None:
        pm = get_plugin_manager()

    hook_caller = getattr(pm.hook, hook_attr)  # AttributeError = bug in calling code
    try:
        provided = hook_caller()
    except Exception:
        # Individual plugin hook implementations can raise arbitrary errors.
        # One bad plugin shouldn't crash the caller — log and return empty.
        logger.exception("Failed to resolve %s plugins", label)
        return []

    results: list[Any] = []
    for item in provided:
        if item is None:
            continue
        if not validator(item):
            logger.warning(
                "Ignoring invalid %s plugin",
                label,
                plugin_type=type(item).__name__,
            )
            continue
        results.append(item)
    return results
