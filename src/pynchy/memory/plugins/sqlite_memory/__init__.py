"""SQLite memory plugin — provides memory backend + MCP service handlers."""

from __future__ import annotations

from typing import Any

import pluggy

from pynchy.logger import logger

from .backend import SqliteMemoryBackend

hookimpl = pluggy.HookimplMarker("pynchy")

# Singleton backend instance shared between both hooks.
_backend: SqliteMemoryBackend | None = None


def _get_backend() -> SqliteMemoryBackend:
    global _backend  # noqa: PLW0603
    if _backend is None:
        _backend = SqliteMemoryBackend()
    return _backend


# ---------------------------------------------------------------------------
# MCP service handlers (called by host IPC dispatcher)
# ---------------------------------------------------------------------------


async def _handle_save_memory(data: dict) -> dict:
    source_group = data.get("source_group")
    if not source_group:
        return {"error": "Missing source_group"}

    key = data.get("key")
    content = data.get("content")
    if not key or not content:
        return {"error": "Missing required fields: key, content"}

    backend = _get_backend()
    result = await backend.save(
        group_folder=source_group,
        key=key,
        content=content,
        category=data.get("category", "core"),
        metadata=data.get("metadata"),
    )
    return {"result": result}


async def _handle_recall_memories(data: dict) -> dict:
    source_group = data.get("source_group")
    if not source_group:
        return {"error": "Missing source_group"}

    query = data.get("query")
    if not query:
        return {"error": "Missing required field: query"}

    backend = _get_backend()
    results = await backend.recall(
        group_folder=source_group,
        query=query,
        category=data.get("category"),
        limit=data.get("limit", 5),
    )
    return {"result": {"memories": results, "count": len(results)}}


async def _handle_forget_memory(data: dict) -> dict:
    source_group = data.get("source_group")
    if not source_group:
        return {"error": "Missing source_group"}

    key = data.get("key")
    if not key:
        return {"error": "Missing required field: key"}

    backend = _get_backend()
    result = await backend.forget(group_folder=source_group, key=key)
    return {"result": result}


async def _handle_list_memories(data: dict) -> dict:
    source_group = data.get("source_group")
    if not source_group:
        return {"error": "Missing source_group"}

    backend = _get_backend()
    results = await backend.list_keys(
        group_folder=source_group,
        category=data.get("category"),
    )
    return {"result": {"memories": results, "count": len(results)}}


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------


class SqliteMemoryPlugin:
    """Plugin providing SQLite FTS5-backed persistent memory."""

    @hookimpl
    def pynchy_memory(self) -> Any | None:
        backend = _get_backend()
        logger.debug("SQLite memory backend provided")
        return backend

    @hookimpl
    def pynchy_mcp_server_handler(self) -> dict[str, Any]:
        return {
            "tools": {
                "save_memory": _handle_save_memory,
                "recall_memories": _handle_recall_memories,
                "forget_memory": _handle_forget_memory,
                "list_memories": _handle_list_memories,
            },
        }
