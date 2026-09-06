"""Shared IPC utilities for agent tools."""

from __future__ import annotations

import json
import os
import secrets
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

IPC_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class AgentToolRuntime:
    """Runtime identity and IPC location for an agent-tools MCP server."""

    chat_jid: str
    group_folder: str
    is_admin: bool
    is_scheduled_task: bool
    ipc_dir: Path
    service_request_timeout_seconds: float = 300.0
    ask_user_timeout_seconds: float = 1800.0
    turn_id: str = ""

    @classmethod
    def from_environment(cls) -> AgentToolRuntime:
        """Build the normal in-container runtime from the runner environment."""
        return cls(
            chat_jid=os.environ.get("PYNCHY_CHAT_JID", ""),
            group_folder=os.environ.get("PYNCHY_GROUP_FOLDER", ""),
            is_admin=os.environ.get("PYNCHY_IS_ADMIN") == "1",
            is_scheduled_task=os.environ.get("PYNCHY_IS_SCHEDULED_TASK") == "1",
            ipc_dir=Path(os.environ.get("PYNCHY_IPC_DIR", "/run/pynchy")),
            turn_id=os.environ.get("PYNCHY_TURN_ID", ""),
        )


_runtime: AgentToolRuntime = AgentToolRuntime.from_environment()


def get_agent_tool_runtime() -> AgentToolRuntime:
    """Return the context currently used by the agent-tools MCP server."""
    return _runtime


@contextmanager
def use_agent_tool_runtime(runtime: AgentToolRuntime) -> Iterator[None]:
    """Temporarily run public agent-tool operations with *runtime* context.

    The default context comes from the container environment.  Embedders can
    use this scope when serving a request on behalf of another workspace.
    Calls must not overlap because MCP tool registration has process-global
    context today.
    """
    global _runtime  # noqa: PLW0603 - process-wide singleton.
    previous = get_agent_tool_runtime()
    _runtime = runtime
    try:
        yield
    finally:
        _runtime = previous


def write_ipc_file(directory: Path, data: dict[str, Any]) -> str:
    """Write an IPC file atomically (temp file + rename)."""
    directory.mkdir(parents=True, exist_ok=True)

    filename = f"{int(time.time() * 1000)}-{secrets.token_hex(3)}.json"
    filepath = directory / filename

    temp_path = filepath.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(data, indent=2))
    temp_path.rename(filepath)

    return filename


def make_ipc_request(
    kind: str,
    payload: dict[str, Any],
    *,
    request_id: str | None = None,
    reply_to: str | None = "responses",
    deadline: str | None = None,
) -> dict[str, Any]:
    """Build a canonical IPC request envelope for host-bound operations."""
    return {
        "schema_version": IPC_SCHEMA_VERSION,
        "kind": kind,
        "request_id": request_id or uuid.uuid4().hex,
        "source_group": get_agent_tool_runtime().group_folder,
        "created_at": now_iso(),
        "reply_to": reply_to,
        "deadline": deadline,
        "payload": payload,
    }


def write_request_file(
    kind: str,
    payload: dict[str, Any],
    *,
    request_id: str | None = None,
    reply_to: str | None = "responses",
    deadline: str | None = None,
) -> tuple[str, str]:
    """Write a canonical request file and return (filename, request_id)."""
    envelope = make_ipc_request(
        kind,
        payload,
        request_id=request_id,
        reply_to=reply_to,
        deadline=deadline,
    )
    filename = write_ipc_file(get_agent_tool_runtime().ipc_dir / "requests", envelope)
    return filename, envelope["request_id"]


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
