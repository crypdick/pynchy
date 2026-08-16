"""Kubernetes runtime provider."""

from __future__ import annotations

import json
import shutil
import subprocess  # noqa: S404 - trusted fixed kubectl argv.

from .cli import kubectl_command, pod_name


class KubernetesContainerRuntime:
    """Container runtime backed by namespace-scoped Kubernetes Pods."""

    name = "kubernetes"
    cli = "pynchy-kubernetes-runtime"

    def is_available(self) -> bool:
        return shutil.which("kubectl") is not None and shutil.which(self.cli) is not None

    def ensure_running(self) -> None:
        subprocess.run(  # noqa: S603 - fixed kubectl capability probe.
            [
                *kubectl_command(),
                "get",
                "pods",
                "-l",
                "app.kubernetes.io/managed-by=pynchy",
                "--request-timeout=10s",
            ],
            capture_output=True,
            check=True,
            timeout=15,
        )

    def list_running_containers(self, prefix: str = "pynchy-") -> list[str]:
        result = subprocess.run(  # noqa: S603 - fixed kubectl inventory query.
            [
                *kubectl_command(),
                "get",
                "pods",
                "-l",
                "app.kubernetes.io/managed-by=pynchy",
                "-o",
                "json",
            ],
            capture_output=True,
            check=True,
            text=True,
            timeout=15,
        )
        names: list[str] = []
        for item in json.loads(result.stdout).get("items", []):
            metadata = item.get("metadata", {})
            runtime_name = metadata.get("annotations", {}).get("pynchy.dev/runtime-name", "")
            # Pending is alive: the adapter CLI can exit before kubelet starts the Pod.
            phase = item.get("status", {}).get("phase")
            if phase in {"Pending", "Running"} and runtime_name.startswith(prefix):
                names.append(runtime_name)
        return names

    def remove_container(self, name: str, *, force: bool = True) -> bool:
        grace_period = "0" if force else "5"
        result = subprocess.run(  # noqa: S603 - fixed kubectl deletion.
            [
                *kubectl_command(),
                "delete",
                "pod",
                pod_name(name),
                f"--grace-period={grace_period}",
                "--ignore-not-found",
            ],
            capture_output=True,
            check=False,
            timeout=15,
        )
        return result.returncode == 0
