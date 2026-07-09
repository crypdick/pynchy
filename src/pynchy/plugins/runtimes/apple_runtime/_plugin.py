"""Apple Container runtime plugin."""

from __future__ import annotations

import pluggy

from .runtime import AppleContainerRuntime

hookimpl = pluggy.HookimplMarker("pynchy")


class AppleRuntimePlugin:
    """Plugin providing Apple Container runtime detection."""

    @hookimpl
    def pynchy_container_runtime(self) -> AppleContainerRuntime:
        return AppleContainerRuntime()
