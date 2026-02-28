"""Plugin system — registry, hookspecs, and all plugin implementations."""

from pynchy.plugins.registry import (  # noqa: F401
    _BUILTIN_PLUGIN_SPECS,
    collect_hook_results,
    get_plugin_manager,
)
