"""Plugin connection-runtime loading contracts."""

from __future__ import annotations

import pluggy

from pynchy.plugins.api import PynchySpec, load_connection_runtimes

hookimpl = pluggy.HookimplMarker("pynchy")


class _NoConnectionRuntimePlugin:
    @hookimpl
    def pynchy_connection_runtime(self) -> None:
        return None


def test_loader_ignores_plugins_without_connection_runtimes() -> None:
    manager = pluggy.PluginManager("pynchy")
    manager.add_hookspecs(PynchySpec)
    manager.register(_NoConnectionRuntimePlugin())

    assert load_connection_runtimes(manager) == []
