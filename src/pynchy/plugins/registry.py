"""Plugin system for pynchy.

Plugins extend pynchy with external capabilities like agent cores,
communication channels, MCP tools, skills, and managed workspaces.

Built on pluggy (pytest's plugin framework) for robust, type-safe plugin management.

Usage:
    from pynchy.plugins import get_plugin_manager

    pm = get_plugin_manager()
    cores = pm.hook.pynchy_agent_core_info()  # List of AgentCoreSpec objects
    channels = pm.hook.pynchy_create_channel(context=ctx)  # All matching channels
"""

from __future__ import annotations

import asyncio
import importlib
import warnings
from collections.abc import (
    Callable,  # noqa: TC003 - beartype resolves this runtime annotation.
    Mapping,  # noqa: TC003 - beartype resolves this runtime annotation.
)
from typing import TypeGuard

import pluggy

from pynchy.logger import logger
from pynchy.plugins.hookspecs import PynchySpec

__all__ = [
    "collect_hook_results",
    "get_plugin_manager",
]

# Static registry of built-in plugins.
# Each entry: (module_path, class_name, config_key)
# config_key is checked against layered [plugins.<key>].enabled settings.
_BUILTIN_PLUGIN_SPECS: list[tuple[str, str, str]] = [
    ("pynchy.plugins.agent_cores.claude", "ClaudeAgentCorePlugin", "claude"),
    ("pynchy.plugins.agent_cores.claude_cli", "ClaudeCLIAgentCorePlugin", "claude-cli"),
    ("pynchy.plugins.agent_cores.openai", "OpenAIAgentCorePlugin", "openai"),
    ("pynchy.plugins.agent_cores.codex", "CodexAgentCorePlugin", "codex"),
    ("pynchy.plugins.channels.discord", "DiscordChannelPlugin", "discord"),
    ("pynchy.plugins.channels.slack", "SlackChannelPlugin", "slack"),
    ("pynchy.plugins.channels.whatsapp", "WhatsAppPlugin", "whatsapp"),
    ("pynchy.plugins.speech.pocket_tts", "PocketTtsPlugin", "pocket-tts"),
    ("pynchy.plugins.tunnels.tailscale", "TailscaleTunnelPlugin", "tailscale"),
    ("pynchy.plugins.runtimes.docker_runtime", "DockerRuntimePlugin", "docker-runtime"),
    ("pynchy.plugins.runtimes.apple_runtime", "AppleRuntimePlugin", "apple-runtime"),
    (
        "pynchy.plugins.runtimes.kubernetes_runtime",
        "KubernetesRuntimePlugin",
        "kubernetes-runtime",
    ),
    ("pynchy.plugins.integrations.caldav", "CalDAVMcpServerPlugin", "caldav"),
    (
        "pynchy.plugins.integrations.slack_token_extractor",
        "SlackTokenExtractorPlugin",
        "slack-token-extractor",
    ),
    (
        "pynchy.plugins.integrations.x_integration",
        "XIntegrationPlugin",
        "x-integration",
    ),
    (
        "pynchy.plugins.integrations.google_setup",
        "GoogleMcpPlugin",
        "google",
    ),
    (
        "pynchy.plugins.integrations.google_setup",
        "GoogleSetupPlugin",
        "google-setup",
    ),
    ("pynchy.plugins.integrations.gog", "GogWorkspacePlugin", "gog"),
    (
        "pynchy.plugins.integrations.playwright_browser",
        "PlaywrightBrowserPlugin",
        "playwright-browser",
    ),
    (
        "pynchy.plugins.integrations.desktop_screenshot",
        "DesktopScreenshotPlugin",
        "desktop-screenshot",
    ),
    (
        "pynchy.plugins.integrations.computer_use",
        "ComputerUsePlugin",
        "computer-use",
    ),
    (
        "pynchy.plugins.integrations.peekaboo",
        "PeekabooComputerUsePlugin",
        "peekaboo",
    ),
    (
        "pynchy.plugins.integrations.cua_driver",
        "CuaDriverComputerUsePlugin",
        "cua-driver",
    ),
    (
        "pynchy.plugins.integrations.ssh_x11",
        "SshX11ComputerUsePlugin",
        "ssh-x11",
    ),
    (
        "pynchy.plugins.integrations.linux_x11",
        "LinuxX11ComputerUsePlugin",
        "linux-x11",
    ),
    (
        "pynchy.plugins.integrations.linear",
        "LinearMcpPlugin",
        "linear",
    ),
    (
        "pynchy.plugins.integrations.proton_mail",
        "ProtonMailMcpPlugin",
        "proton-mail",
    ),
    (
        "pynchy.plugins.integrations.matrix_gateway",
        "MatrixGatewayPlugin",
        "matrix-gateway",
    ),
    (
        "pynchy.plugins.integrations.marketplace_health",
        "MarketplaceHealthPlugin",
        "marketplace-health",
    ),
    (
        "pynchy.plugins.integrations.vaultwarden",
        "VaultwardenPlugin",
        "vaultwarden",
    ),
    (
        "pynchy.plugins.integrations.notebook_server",
        "NotebookServerPlugin",
        "notebook",
    ),
    ("pynchy.plugins.observers.sqlite_observer", "SqliteObserverPlugin", "sqlite-observer"),
]


def _set_import_event_loop() -> asyncio.AbstractEventLoop | None:
    """Install a temporary loop for import-time `get_event_loop()` calls."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            asyncio.set_event_loop(loop)
        return loop
    return None


def _clear_import_event_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """Close the temporary import loop if one was installed."""
    if loop is None:
        return
    loop.close()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        asyncio.set_event_loop(None)


def _register_builtin_plugins(
    pm: pluggy.PluginManager, enabled_plugins: Mapping[str, bool]
) -> None:
    """Register built-in plugins from the static registry."""
    for module_path, class_name, config_key in _BUILTIN_PLUGIN_SPECS:
        if enabled_plugins.get(config_key) is False:
            logger.info("Plugin disabled via config", plugin=config_key)
            continue
        try:
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
            pm.register(cls(), name=f"builtin-{config_key}")
            logger.info("Registered built-in plugin", name=config_key)
        except ImportError:
            logger.debug("Plugin skipped (optional dependency missing)", plugin=config_key)
        except Exception:  # noqa: BLE001 - built-in plugin import/load isolation.
            logger.exception(
                "Failed to load built-in plugin",
                plugin=config_key,
            )


def _unregister_class_plugins(pm: pluggy.PluginManager) -> None:
    """Drop invalid entrypoint registrations that provide classes, not instances."""
    for plugin in list(pm.get_plugins()):
        if not isinstance(plugin, type):
            continue
        plugin_name = pm.get_name(plugin) or plugin.__name__
        pm.unregister(plugin=plugin)
        logger.warning(
            "Unregistered invalid class-based plugin object",
            plugin=plugin_name,
        )


def _log_plugin_summary(pm: pluggy.PluginManager, discovered: int) -> None:
    """Emit discovery and final plugin inventory logs."""
    if discovered:
        logger.info("Discovered third-party plugins", count=discovered)
    plugin_names = [pm.get_name(plugin) for plugin in pm.get_plugins()]
    logger.info("Plugin manager ready", plugins=plugin_names)


def get_plugin_manager(enabled_plugins: Mapping[str, bool] | None = None) -> pluggy.PluginManager:
    """Create and configure the plugin manager.

    Discovers plugins from the static registry and entry points.
    All hook specifications are validated at registration time.

    Returns:
        Configured PluginManager ready to call hooks
    """
    pm = pluggy.PluginManager("pynchy")
    pm.add_hookspecs(PynchySpec)

    # Some plugins call `asyncio.get_event_loop()` at import time. Install a
    # temporary default loop so imports in sync/xdist contexts don't auto-create
    # orphan loops we never close ourselves.
    tmp_loop = _set_import_event_loop()
    try:
        _register_builtin_plugins(pm, enabled_plugins or {})
        discovered = pm.load_setuptools_entrypoints("pynchy")
    finally:
        _clear_import_event_loop(tmp_loop)

    _unregister_class_plugins(pm)
    _log_plugin_summary(pm, discovered)

    return pm


def collect_hook_results[T](
    hook_attr: str,
    validator: Callable[[object], TypeGuard[T]],
    label: str,
    *,
    pm: pluggy.PluginManager | None = None,
    **hook_kwargs: object,
) -> list[T]:
    """Call a pluggy hook and return validated results.

    Handles plugin manager retrieval, hook invocation, and filtering
    through a validator function.  Invalid or ``None`` results are
    logged and skipped.

    Args:
        hook_attr: Name of the hook attribute on ``pm.hook``.
        validator: Callable that returns ``True`` for valid hook results.
        label: Human-readable label for log messages.
        pm: Optional pre-existing plugin manager.  If ``None``, calls
            :func:`get_plugin_manager`.
    """
    if pm is None:
        pm = get_plugin_manager()

    hook_caller = getattr(pm.hook, hook_attr)  # AttributeError = bug in calling code
    try:
        provided = hook_caller(**hook_kwargs)
    except Exception:  # noqa: BLE001 - plugin hook isolation; one bad plugin shouldn't crash the caller.
        # Individual plugin hook implementations can raise arbitrary errors.
        # One bad plugin shouldn't crash the caller — log and return empty.
        logger.exception("Failed to resolve %s plugins", label)
        return []

    results: list[T] = []
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
