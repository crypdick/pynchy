"""Container runtime detection with plugin-extensible providers.

Docker is built in. Additional runtimes (for example Apple Container)
can be provided by plugins via ``pynchy_container_runtime``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pynchy.logger import logger


@runtime_checkable
class RuntimeProvider(Protocol):
    """Runtime provider contract implemented by built-ins and plugins."""

    name: str
    cli: str

    def is_available(self) -> bool: ...
    def ensure_running(self) -> None: ...
    def list_running_containers(self, prefix: str = "pynchy-") -> list[str]: ...


@dataclass(frozen=True)
class ContainerRuntime:
    """Built-in runtime implementation wrapper."""

    name: str
    cli: str
    _available: Any
    _ensure: Any
    _list: Any

    def is_available(self) -> bool:
        return bool(self._available())

    def ensure_running(self) -> None:
        self._ensure()

    def list_running_containers(self, prefix: str = "pynchy-") -> list[str]:
        try:
            return self._list(prefix)
        except Exception as exc:
            logger.warning("Failed to list containers", err=str(exc), runtime=self.name)
            return []


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _ensure_docker() -> None:
    try:
        subprocess.run(
            ["docker", "info"],
            capture_output=True,
            check=True,
        )
        logger.debug("Docker daemon is running")
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        if sys.platform == "darwin":
            _start_docker_desktop(exc)
        else:
            raise RuntimeError(
                "Docker is required but not running. Start with: sudo systemctl start docker"
            ) from exc


def _start_docker_desktop(original_exc: Exception) -> None:
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


def _list_docker(prefix: str) -> list[str]:
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


def _docker_runtime() -> ContainerRuntime:
    return ContainerRuntime(
        name="docker",
        cli="docker",
        _available=_docker_available,
        _ensure=_ensure_docker,
        _list=_list_docker,
    )


def _is_valid_plugin_runtime(candidate: Any) -> bool:
    return all(
        [
            hasattr(candidate, "name"),
            hasattr(candidate, "cli"),
            callable(getattr(candidate, "is_available", None)),
            callable(getattr(candidate, "ensure_running", None)),
            callable(getattr(candidate, "list_running_containers", None)),
        ]
    )


def _iter_plugin_runtimes() -> list[RuntimeProvider]:
    try:
        from pynchy.plugin import get_plugin_manager
    except Exception:
        logger.exception("Failed to import plugin manager while loading runtime plugins")
        return []

    try:
        pm = get_plugin_manager()
        provided = pm.hook.pynchy_container_runtime()
    except Exception:
        logger.exception("Failed to resolve runtime plugins")
        return []

    runtimes: list[RuntimeProvider] = []
    for runtime in provided:
        if runtime is None:
            continue
        if not _is_valid_plugin_runtime(runtime):
            logger.warning(
                "Ignoring invalid plugin runtime object",
                runtime_type=type(runtime).__name__,
            )
            continue
        runtimes.append(runtime)
    return runtimes


def detect_runtime() -> RuntimeProvider:
    """Detect the container runtime to use.

    Priority:
    1) settings.container.runtime override (if available)
    2) platform-aware auto-detect (darwin prefers apple plugin, then docker)
    3) first available plugin runtime, then docker fallback
    """
    from pynchy.config import get_settings

    override = (get_settings().container.runtime or "").lower()
    docker = _docker_runtime()
    candidates: dict[str, RuntimeProvider] = {"docker": docker}
    for runtime in _iter_plugin_runtimes():
        name = str(runtime.name).lower().strip()
        if not name:
            continue
        if name in candidates:
            logger.warning("Duplicate runtime provider ignored", runtime=name)
            continue
        candidates[name] = runtime

    if override:
        selected = candidates.get(override)
        if selected is not None:
            return selected
        logger.warning("Unknown runtime override; falling back to auto-detection", runtime=override)

    if sys.platform == "darwin":
        apple = candidates.get("apple")
        if apple and apple.is_available():
            return apple
        if docker.is_available():
            if apple is None:
                logger.info(
                    "Apple runtime plugin not installed, falling back to Docker. "
                    "Enable a plugin that implements pynchy_container_runtime for Apple support."
                )
            else:
                logger.info("Apple runtime unavailable, falling back to Docker")
            return docker
        if apple is not None:
            return apple

    for name, runtime in candidates.items():
        if name == "docker":
            continue
        if runtime.is_available():
            return runtime

    return docker


_runtime: RuntimeProvider | None = None


def get_runtime() -> RuntimeProvider:
    """Lazy singleton — caches the result of detect_runtime()."""
    global _runtime  # noqa: PLW0603
    if _runtime is None:
        _runtime = detect_runtime()
        logger.info("Container runtime detected", name=_runtime.name, cli=_runtime.cli)
    return _runtime
