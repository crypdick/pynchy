"""SQLite memory plugin — provides memory backend + MCP service handlers."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003, RUF100 - beartype resolves this runtime annotation.
from typing import Any

import pluggy

from pynchy.actions import ActionId
from pynchy.capabilities import (
    ApprovalContract,
    ApprovalTrigger,
    AuditContract,
    CapabilityDescriptor,
    CapabilityId,
    CapabilityKind,
    HostActionAccess,
    HostActionDescriptor,
    HostActionHandler,
    HostActionRegistration,
    HostToolName,
    IdempotencyContract,
    IdempotencyMode,
)
from pynchy.logger import logger
from pynchy.types import ServiceTrustConfig

from .backend import SqliteMemoryBackend

__all__ = ["SqliteMemoryPlugin"]

hookimpl = pluggy.HookimplMarker("pynchy")
type _ActionDefinition = tuple[str, str, str, HostActionAccess]
_DEFAULT_CATEGORY = "core"
_DEFAULT_LIMIT = 5
_MEMORY_TRUST = ServiceTrustConfig(
    public_source=False,
    secret_data=True,
    public_sink=False,
    dangerous_writes=False,
)


# ---------------------------------------------------------------------------
# MCP service handlers (called by host IPC dispatcher)
# ---------------------------------------------------------------------------


async def _save_memory(backend: SqliteMemoryBackend, data: dict[str, Any]) -> dict[str, Any]:
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

    result = await backend.save(
        group_folder=source_group,
        key=key,
        content=content,
        category=category,
        metadata=metadata,
    )
    return {"result": result}


async def _recall_memories(backend: SqliteMemoryBackend, data: dict[str, Any]) -> dict[str, Any]:
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

    results = await backend.recall(
        group_folder=source_group,
        query=query,
        category=category,
        limit=limit,
    )
    return {"result": {"memories": results, "count": len(results)}}


async def _forget_memory(backend: SqliteMemoryBackend, data: dict[str, Any]) -> dict[str, Any]:
    source_group = data.get("source_group")
    if not isinstance(source_group, str) or not source_group:
        return {"error": "Missing source_group"}

    key = data.get("key")
    if not isinstance(key, str) or not key:
        return {"error": "Missing required field: key"}

    result = await backend.forget(group_folder=source_group, key=key)
    return {"result": result}


async def _list_memories(backend: SqliteMemoryBackend, data: dict[str, Any]) -> dict[str, Any]:
    source_group = data.get("source_group")
    if not isinstance(source_group, str) or not source_group:
        return {"error": "Missing source_group"}
    category = data.get("category")
    if category is not None and not isinstance(category, str):
        return {"error": "category must be a string"}

    results = await backend.list_keys(
        group_folder=source_group,
        category=category,
    )
    return {"result": {"memories": results, "count": len(results)}}


_MEMORY_ACTIONS: tuple[_ActionDefinition, ...] = (
    (
        "save_memory",
        "memory.save",
        "Create or update a memory in this workspace's isolated store.",
        HostActionAccess.WRITE,
    ),
    (
        "recall_memories",
        "memory.recall",
        "Search this workspace's isolated memories.",
        HostActionAccess.READ,
    ),
    (
        "forget_memory",
        "memory.forget",
        "Delete a memory from this workspace's isolated store.",
        HostActionAccess.WRITE,
    ),
    (
        "list_memories",
        "memory.list",
        "List keys in this workspace's isolated memory store.",
        HostActionAccess.READ,
    ),
)


def _memory_action(
    definition: _ActionDefinition, handler: HostActionHandler
) -> HostActionDescriptor:
    tool_name, action_id, summary, access = definition
    return HostActionDescriptor(
        capability=CapabilityDescriptor(
            id=CapabilityId(action_id),
            kind=CapabilityKind.HOST_ACTION,
            owner="sqlite-memory",
            summary=summary,
            action_ids=(ActionId(action_id),),
            documentation="docs/usage/memory.md",
        ),
        tool_name=HostToolName(tool_name),
        handler=handler,
        access=access,
        approval=ApprovalContract(trigger=ApprovalTrigger.CAPABILITY_ONLY),
        idempotency=IdempotencyContract(
            IdempotencyMode.NOT_REQUIRED
            if access is HostActionAccess.READ
            else IdempotencyMode.IPC_REQUEST_ID
        ),
        audit=AuditContract(),
        policy_service="sqlite-memory",
        default_service_trust=_MEMORY_TRUST,
    )


class SqliteMemoryPlugin:
    """Plugin providing SQLite FTS5-backed persistent memory."""

    def __init__(self, backend: SqliteMemoryBackend | None = None) -> None:
        self._backend = backend

    def _require_backend(self) -> SqliteMemoryBackend:
        if self._backend is None:
            raise RuntimeError(
                "SQLite memory backend is unavailable before application initialization"
            )
        return self._backend

    @hookimpl
    def pynchy_memory(self, database_path: Path) -> SqliteMemoryBackend:
        if self._backend is None:
            self._backend = SqliteMemoryBackend(database_path)
        backend = self._backend
        logger.debug("SQLite memory backend provided")
        return backend

    @hookimpl
    def pynchy_service_handler(self) -> HostActionRegistration:
        return HostActionRegistration(
            actions=(
                _memory_action(_MEMORY_ACTIONS[0], self._handle_save_memory),
                _memory_action(_MEMORY_ACTIONS[1], self._handle_recall_memories),
                _memory_action(_MEMORY_ACTIONS[2], self._handle_forget_memory),
                _memory_action(_MEMORY_ACTIONS[3], self._handle_list_memories),
            )
        )

    async def _handle_save_memory(self, data: dict[str, Any]) -> dict[str, Any]:
        return await _save_memory(self._require_backend(), data)

    async def _handle_recall_memories(self, data: dict[str, Any]) -> dict[str, Any]:
        return await _recall_memories(self._require_backend(), data)

    async def _handle_forget_memory(self, data: dict[str, Any]) -> dict[str, Any]:
        return await _forget_memory(self._require_backend(), data)

    async def _handle_list_memories(self, data: dict[str, Any]) -> dict[str, Any]:
        return await _list_memories(self._require_backend(), data)
