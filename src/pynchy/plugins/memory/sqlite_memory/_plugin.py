"""SQLite memory plugin — provides memory backend + MCP service handlers."""

from __future__ import annotations

from functools import cache

import pluggy

from pynchy.logger import logger

from .backend import SqliteMemoryBackend

__all__ = ["SqliteMemoryPlugin"]

hookimpl = pluggy.HookimplMarker("pynchy")
_DEFAULT_CATEGORY = "core"
_DEFAULT_LIMIT = 5


@cache
def _get_backend() -> SqliteMemoryBackend:
    return SqliteMemoryBackend()


# ---------------------------------------------------------------------------
# MCP service handlers (called by host IPC dispatcher)
# ---------------------------------------------------------------------------


async def _handle_save_memory(data: dict[str, object]) -> dict[str, object]:
    source_group = data.get("source_group")
    if not isinstance(source_group, str) or not source_group:
        return {"error": "Missing source_group"}

    key = data.get("key")
    content = data.get("content")
    if not isinstance(key, str) or not key or not isinstance(content, str) or not content:
        return {"error": "Missing required fields: key, content"}
    category = data.get("category", _DEFAULT_CATEGORY)
    if not isinstance(category, str):
        return {"error": "category must be a string"}
    metadata = data.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        return {"error": "metadata must be an object"}

    backend = _get_backend()
    result = await backend.save(
        group_folder=source_group,
        key=key,
        content=content,
        category=category,
        metadata=metadata,
    )
    return {"result": result}


async def _handle_recall_memories(data: dict[str, object]) -> dict[str, object]:
    source_group = data.get("source_group")
    if not isinstance(source_group, str) or not source_group:
        return {"error": "Missing source_group"}

    query = data.get("query")
    if not isinstance(query, str) or not query:
        return {"error": "Missing required field: query"}
    category = data.get("category")
    if category is not None and not isinstance(category, str):
        return {"error": "category must be a string"}
    limit = data.get("limit", _DEFAULT_LIMIT)
    if not isinstance(limit, int):
        return {"error": "limit must be an integer"}

    backend = _get_backend()
    results = await backend.recall(
        group_folder=source_group,
        query=query,
        category=category,
        limit=limit,
    )
    return {"result": {"memories": results, "count": len(results)}}


async def _handle_forget_memory(data: dict[str, object]) -> dict[str, object]:
    source_group = data.get("source_group")
    if not isinstance(source_group, str) or not source_group:
        return {"error": "Missing source_group"}

    key = data.get("key")
    if not isinstance(key, str) or not key:
        return {"error": "Missing required field: key"}

    backend = _get_backend()
    result = await backend.forget(group_folder=source_group, key=key)
    return {"result": result}


async def _handle_list_memories(data: dict[str, object]) -> dict[str, object]:
    source_group = data.get("source_group")
    if not isinstance(source_group, str) or not source_group:
        return {"error": "Missing source_group"}
    category = data.get("category")
    if category is not None and not isinstance(category, str):
        return {"error": "category must be a string"}

    backend = _get_backend()
    results = await backend.list_keys(
        group_folder=source_group,
        category=category,
    )
    return {"result": {"memories": results, "count": len(results)}}


class SqliteMemoryPlugin:
    """Plugin providing SQLite FTS5-backed persistent memory."""

    @hookimpl
    def pynchy_memory(self) -> SqliteMemoryBackend:
        backend = _get_backend()
        logger.debug("SQLite memory backend provided")
        return backend

    @hookimpl
    def pynchy_service_handler(self) -> dict[str, object]:
        return {
            "tools": {
                "save_memory": _handle_save_memory,
                "recall_memories": _handle_recall_memories,
                "forget_memory": _handle_forget_memory,
                "list_memories": _handle_list_memories,
            },
        }
