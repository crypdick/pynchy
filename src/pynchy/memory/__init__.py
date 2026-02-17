"""Memory subsystem for pynchy.

Provides persistent, searchable memory storage per workspace.
Built-in backends live under ``memory/plugins/``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pynchy.logger import logger


@runtime_checkable
class MemoryProvider(Protocol):
    """Memory provider contract implemented by plugins."""

    name: str

    async def init(self) -> None: ...

    async def save(
        self,
        group_folder: str,
        key: str,
        content: str,
        category: str = "core",
        metadata: dict | None = None,
    ) -> dict: ...

    async def recall(
        self,
        group_folder: str,
        query: str,
        category: str | None = None,
        limit: int = 5,
    ) -> list[dict]: ...

    async def forget(self, group_folder: str, key: str) -> dict: ...

    async def list_keys(
        self,
        group_folder: str,
        category: str | None = None,
    ) -> list[dict]: ...

    async def close(self) -> None: ...


def _is_valid_provider(candidate: Any) -> bool:
    return all(
        [
            hasattr(candidate, "name"),
            callable(getattr(candidate, "init", None)),
            callable(getattr(candidate, "save", None)),
            callable(getattr(candidate, "recall", None)),
            callable(getattr(candidate, "forget", None)),
            callable(getattr(candidate, "list_keys", None)),
            callable(getattr(candidate, "close", None)),
        ]
    )


def get_memory_provider() -> MemoryProvider | None:
    """Discover memory plugin and return provider (first valid one wins)."""
    try:
        from pynchy.plugin import get_plugin_manager
    except Exception:
        logger.exception("Failed to import plugin manager while loading memory provider")
        return None

    try:
        pm = get_plugin_manager()
        provided = pm.hook.pynchy_memory()
    except Exception:
        logger.exception("Failed to resolve memory plugins")
        return None

    for provider in provided:
        if provider is None:
            continue
        if not _is_valid_provider(provider):
            logger.warning(
                "Ignoring invalid memory plugin object",
                provider_type=type(provider).__name__,
            )
            continue
        logger.info("Memory provider discovered", name=provider.name)
        return provider

    return None
