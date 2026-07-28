"""Memory subsystem for pynchy.

Provides persistent, searchable memory storage per workspace.
Built-in backends live under ``plugins/memory/``.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003, RUF100 - beartype resolves this runtime annotation.
from typing import TYPE_CHECKING, Protocol, TypeGuard, runtime_checkable

import pluggy  # noqa: TC002, RUF100 - beartype resolves plugin-manager annotations at runtime.

from pynchy.logger import logger

if TYPE_CHECKING:
    from pynchy.identifiers import GroupFolder

__all__ = ["MemoryProvider", "get_memory_provider"]


@runtime_checkable
class MemoryProvider(Protocol):
    """Memory provider contract implemented by plugins."""

    name: str

    async def init(self) -> None: ...

    async def save(
        self,
        group_folder: GroupFolder,
        key: str,
        content: str,
        category: str = "core",
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]: ...

    async def recall(
        self,
        group_folder: GroupFolder,
        query: str,
        category: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, object]]: ...

    async def forget(self, group_folder: GroupFolder, key: str) -> dict[str, object]: ...

    async def list_keys(
        self,
        group_folder: GroupFolder,
        category: str | None = None,
    ) -> list[dict[str, object]]: ...

    async def close(self) -> None: ...


def _is_valid_provider(candidate: object) -> TypeGuard[MemoryProvider]:
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


def get_memory_provider(
    plugin_manager: pluggy.PluginManager,
    database_path: Path,
) -> MemoryProvider | None:
    """Discover memory plugin and return provider (first valid one wins)."""
    try:
        candidates = plugin_manager.hook.pynchy_memory(database_path=database_path)
    except Exception:  # noqa: BLE001, RUF100 - one plugin must not break memory discovery.
        logger.exception("Failed to resolve memory plugins")
        return None
    providers = [candidate for candidate in candidates if _is_valid_provider(candidate)]
    if providers:
        logger.info("Memory provider discovered", name=providers[0].name)
        return providers[0]
    return None
