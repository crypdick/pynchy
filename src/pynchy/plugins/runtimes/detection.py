"""Container runtime detection with plugin-extensible providers.

All runtimes — including Docker — are provided by plugins via
``pynchy_container_runtime``.  Detection picks the best available
runtime based on config overrides and platform heuristics.
"""

from __future__ import annotations

import sys
from functools import cache
from typing import Any, Protocol, runtime_checkable

from pynchy.logger import logger


@runtime_checkable
class RuntimeProvider(Protocol):
    """Runtime provider contract implemented by plugins."""

    name: str
    cli: str

    def is_available(self) -> bool: ...
    def ensure_running(self) -> None: ...
    def list_running_containers(self, prefix: str = "pynchy-") -> list[str]: ...


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
    from pynchy.plugins import collect_hook_results

    return collect_hook_results("pynchy_container_runtime", _is_valid_plugin_runtime, "runtime")


def _runtime_candidates() -> dict[str, RuntimeProvider]:
    candidates: dict[str, RuntimeProvider] = {}
    for runtime in _iter_plugin_runtimes():
        name = str(runtime.name).lower().strip()
        if not name:
            continue
        if name in candidates:
            logger.warning("Duplicate runtime provider ignored", runtime=name)
            continue
        candidates[name] = runtime
    return candidates


def _resolve_override(
    override: str, candidates: dict[str, RuntimeProvider]
) -> RuntimeProvider | None:
    selected = candidates.get(override)
    if selected is not None:
        return selected
    if override:
        logger.warning("Unknown runtime override; falling back to auto-detection", runtime=override)
    return None


def _darwin_runtime(candidates: dict[str, RuntimeProvider]) -> RuntimeProvider | None:
    apple = candidates.get("apple")
    if apple and apple.is_available():
        return apple

    docker = candidates.get("docker")
    if docker and docker.is_available():
        if apple is None:
            logger.info(
                "Apple runtime plugin not installed, falling back to Docker. "
                "Enable a plugin that implements pynchy_container_runtime for Apple support."
            )
        else:
            logger.info("Apple runtime unavailable, falling back to Docker")
        return docker

    return apple


def _fallback_runtime(candidates: dict[str, RuntimeProvider]) -> RuntimeProvider:
    docker = candidates.get("docker")
    if docker is not None:
        return docker

    available = next((runtime for runtime in candidates.values() if runtime.is_available()), None)
    if available is not None:
        return available

    return next(iter(candidates.values()))


def detect_runtime() -> RuntimeProvider:
    """Detect the container runtime to use.

    Priority:
    1) settings.container.runtime override (if available)
    2) platform-aware auto-detect (darwin prefers apple plugin, then docker)
    3) first available plugin runtime, else docker
    """
    from pynchy.config import get_settings

    override = (get_settings().container.runtime or "").lower()
    candidates = _runtime_candidates()
    if not candidates:
        raise RuntimeError(
            "No container runtime plugins available. "
            "Ensure the Docker or Apple runtime plugin is enabled in config.toml."
        )

    selected = _resolve_override(override, candidates)
    if selected is not None:
        return selected

    if sys.platform == "darwin":
        selected = _darwin_runtime(candidates)
        if selected is not None:
            return selected

    return _fallback_runtime(candidates)


@cache
def get_runtime() -> RuntimeProvider:
    """Lazy singleton — caches the result of detect_runtime()."""
    runtime = detect_runtime()
    logger.info("Container runtime detected", name=runtime.name, cli=runtime.cli)
    return runtime
