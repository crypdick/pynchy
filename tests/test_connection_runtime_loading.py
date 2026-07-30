"""Plugin connection-runtime loading contracts."""

from __future__ import annotations

import pluggy

from pynchy.plugins.api import PynchySpec, load_connection_runtimes

hookimpl = pluggy.HookimplMarker("pynchy")


class _NoConnectionRuntimePlugin:
    @hookimpl
    def pynchy_connection_runtime(self) -> None:
        return None


class _Runtime:
    def __init__(self, name: str) -> None:
        self.name = name

    async def start(self, context: object) -> None:
        del context

    async def close(self) -> None:
        return None

    def is_ready(self) -> bool:
        return True


class _BatchPlugin:
    @hookimpl
    def pynchy_connection_runtime(self) -> tuple[_Runtime, ...]:
        return (_Runtime("zeta"), _Runtime("alpha"))


def test_loader_ignores_plugins_without_connection_runtimes() -> None:
    manager = pluggy.PluginManager("pynchy")
    manager.add_hookspecs(PynchySpec)
    manager.register(_NoConnectionRuntimePlugin())

    assert load_connection_runtimes(manager) == []


def test_loader_flattens_and_sorts_runtime_batches() -> None:
    manager = pluggy.PluginManager("pynchy")
    manager.add_hookspecs(PynchySpec)
    manager.register(_BatchPlugin())

    assert [runtime.name for runtime in load_connection_runtimes(manager)] == ["alpha", "zeta"]
