"""Docker container runtime plugin."""

from __future__ import annotations

import pluggy

from .runtime import DockerContainerRuntime

hookimpl = pluggy.HookimplMarker("pynchy")


class DockerRuntimePlugin:
    """Plugin providing Docker container runtime detection."""

    @hookimpl
    def pynchy_container_runtime(self) -> object | None:
        return DockerContainerRuntime()
