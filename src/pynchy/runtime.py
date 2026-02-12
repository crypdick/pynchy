"""Container runtime detection — Apple Container or Docker.

Detects which container CLI is available and provides runtime-specific
helpers for system startup checks and listing running containers.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Literal

from pynchy.logger import logger


@dataclass(frozen=True)
class ContainerRuntime:
    """Detected container runtime (Apple Container or Docker)."""

    name: Literal["apple", "docker"]
    cli: str  # "container" or "docker"

    def ensure_running(self) -> None:
        """Verify the container runtime is available, start if needed."""
        if self.name == "apple":
            self._ensure_apple()
        else:
            self._ensure_docker()

    def list_running_containers(self, prefix: str = "pynchy-") -> list[str]:
        """Return names of running containers matching *prefix*."""
        try:
            if self.name == "apple":
                return self._list_apple(prefix)
            return self._list_docker(prefix)
        except Exception as exc:
            logger.warning("Failed to list containers", err=str(exc))
            return []

    # -- Apple Container ------------------------------------------------

    def _ensure_apple(self) -> None:
        try:
            subprocess.run(
                ["container", "system", "status"],
                capture_output=True,
                check=True,
            )
            logger.debug("Apple Container system already running")
        except (subprocess.CalledProcessError, FileNotFoundError):
            logger.info("Starting Apple Container system...")
            try:
                subprocess.run(
                    ["container", "system", "start"],
                    capture_output=True,
                    check=True,
                    timeout=30,
                )
                logger.info("Apple Container system started")
            except Exception as exc:
                raise RuntimeError(
                    "Apple Container system is required but failed to start"
                ) from exc

    def _list_apple(self, prefix: str) -> list[str]:
        result = subprocess.run(
            ["container", "ls", "--format", "json"],
            capture_output=True,
            text=True,
        )
        containers = json.loads(result.stdout or "[]")
        return [
            c["configuration"]["id"]
            for c in containers
            if c.get("status") == "running"
            and c.get("configuration", {}).get("id", "").startswith(prefix)
        ]

    # -- Docker ---------------------------------------------------------

    def _ensure_docker(self) -> None:
        try:
            subprocess.run(
                ["docker", "info"],
                capture_output=True,
                check=True,
            )
            logger.debug("Docker daemon is running")
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            if sys.platform == "darwin":
                self._start_docker_desktop(exc)
            else:
                raise RuntimeError(
                    "Docker is required but not running. "
                    "Start with: sudo systemctl start docker"
                ) from exc

    def _start_docker_desktop(self, original_exc: Exception) -> None:
        """Attempt to launch Docker Desktop on macOS and wait for the daemon."""
        logger.info("Docker not running, attempting to start Docker Desktop...")
        try:
            subprocess.run(
                ["open", "-a", "Docker"],
                capture_output=True,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            raise RuntimeError(
                "Docker Desktop is required but could not be started. "
                "Install from https://www.docker.com/products/docker-desktop/"
            ) from exc

        import time

        for i in range(30):
            try:
                subprocess.run(
                    ["docker", "info"],
                    capture_output=True,
                    check=True,
                )
                logger.info("Docker Desktop started successfully")
                return
            except (subprocess.CalledProcessError, FileNotFoundError):
                if i % 5 == 0:
                    logger.info("Waiting for Docker Desktop to start...")
                time.sleep(2)

        raise RuntimeError(
            "Docker Desktop was launched but the daemon did not become ready "
            "within 60s. Check Docker Desktop for errors."
        ) from original_exc

    def _list_docker(self, prefix: str) -> list[str]:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
        )
        names: list[str] = []
        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            c = json.loads(line)
            name = c.get("Names", "")
            if name.startswith(prefix):
                names.append(name)
        return names


def detect_runtime() -> ContainerRuntime:
    """Detect the container runtime to use.

    Priority: CONTAINER_RUNTIME env var → platform → shutil.which().
    """
    override = os.environ.get("CONTAINER_RUNTIME", "").lower()
    if override == "apple":
        return ContainerRuntime(name="apple", cli="container")
    if override == "docker":
        return ContainerRuntime(name="docker", cli="docker")

    # macOS prefers Apple Container if available
    if sys.platform == "darwin" and shutil.which("container"):
        return ContainerRuntime(name="apple", cli="container")

    if sys.platform == "darwin" and shutil.which("docker"):
        logger.info(
            "Apple Container not found, falling back to Docker. "
            "For better macOS integration, install Apple Container: "
            "https://developer.apple.com/documentation/apple-containers"
        )
        return ContainerRuntime(name="docker", cli="docker")

    if shutil.which("docker"):
        return ContainerRuntime(name="docker", cli="docker")

    # Fallback: Apple Container on macOS, Docker everywhere else
    if sys.platform == "darwin":
        return ContainerRuntime(name="apple", cli="container")
    return ContainerRuntime(name="docker", cli="docker")


_runtime: ContainerRuntime | None = None


def get_runtime() -> ContainerRuntime:
    """Lazy singleton — caches the result of detect_runtime()."""
    global _runtime  # noqa: PLW0603
    if _runtime is None:
        _runtime = detect_runtime()
        logger.info("Container runtime detected", name=_runtime.name, cli=_runtime.cli)
    return _runtime
