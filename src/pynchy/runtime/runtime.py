"""Container runtime detection with plugin-extensible providers.

All runtimes — including Docker — are provided by plugins via
``pynchy_container_runtime``.  Detection picks the best available
runtime based on config overrides and platform heuristics.
"""

from __future__ import annotations

import sys
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
    candidates: dict[str, RuntimeProvider] = {}
    for runtime in _iter_plugin_runtimes():
        name = str(runtime.name).lower().strip()
        if not name:
            continue
        if name in candidates:
            logger.warning("Duplicate runtime provider ignored", runtime=name)
            continue
        candidates[name] = runtime

    if not candidates:
        raise RuntimeError(
            "No container runtime plugins available. "
            "Ensure the Docker or Apple runtime plugin is enabled in config.toml."
        )

    if override:
        selected = candidates.get(override)
        if selected is not None:
            return selected
        logger.warning("Unknown runtime override; falling back to auto-detection", runtime=override)

    if sys.platform == "darwin":
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
        # Last resort on macOS: return apple if present, even if not available yet
        if apple is not None:
            return apple

    # Non-macOS or macOS fallthrough: prefer docker, then any available plugin
    docker = candidates.get("docker")
    if docker is not None:
        return docker

    for runtime in candidates.values():
        if runtime.is_available():
            return runtime

    # Return first candidate even if not available (will fail at ensure_running)
    return next(iter(candidates.values()))


_runtime: RuntimeProvider | None = None


def get_runtime() -> RuntimeProvider:
    """Lazy singleton — caches the result of detect_runtime()."""
    global _runtime  # noqa: PLW0603
    if _runtime is None:
        _runtime = detect_runtime()
        logger.info("Container runtime detected", name=_runtime.name, cli=_runtime.cli)
    return _runtime
