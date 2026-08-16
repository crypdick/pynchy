"""Kubernetes container runtime plugin."""

from __future__ import annotations

import pluggy

from .runtime import KubernetesContainerRuntime

hookimpl = pluggy.HookimplMarker("pynchy")


class KubernetesRuntimePlugin:
    """Provide pod-backed containers when Pynchy runs inside Kubernetes."""

    @hookimpl
    def pynchy_container_runtime(self) -> KubernetesContainerRuntime:
        return KubernetesContainerRuntime()
