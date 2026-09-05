"""Plugin system — registry, hookspecs, and all plugin implementations.

Registry exports are lazy so core configuration can import a built-in plugin's
owned config model without recursively importing the settings singleton.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from pynchy.plugins.registry import collect_hook_results, get_plugin_manager

__all__ = ["collect_hook_results", "get_plugin_manager"]


def __getattr__(name: str) -> object:  # noqa: V103
    if name in {*__all__, "_BUILTIN_PLUGIN_SPECS"}:
        from pynchy.plugins import registry  # noqa: PLC0415 - intentional lazy boundary.

        return cast("object", getattr(registry, name))
    message = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(message)
