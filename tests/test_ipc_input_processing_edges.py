"""Behavioral coverage for inbound IPC message-file processing."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from conftest_helpers import NullIpcDeps

from pynchy.host.container_manager.ipc.input_processing import (
    classify_queued_ipc_file,
    handle_message_file,
)
from pynchy.workspace.api import WorkspaceProfile

if TYPE_CHECKING:
    from pathlib import Path

    from pynchy.plugins.api import OutboundEvent


class _MessageDeps(NullIpcDeps):
    def __init__(self, workspaces: dict[str, WorkspaceProfile]) -> None:
        self._workspaces = workspaces
        self.broadcasts: list[tuple[str, OutboundEvent]] = []

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return self._workspaces

    async def broadcast_to_channels(self, jid: str, event: OutboundEvent) -> None:
        self.broadcasts.append((jid, event))

    def default_agent_name(self) -> str:
        return "Pynchy"


def _workspace(*, folder: str = "project", is_admin: bool = False) -> WorkspaceProfile:
    return WorkspaceProfile(
        jid="slack:C123",
        name="Project",
        folder=folder,
        trigger="@Pynchy",
        is_admin=is_admin,
    )


def _write_message(path: Path, *, sender: str | None = "Alice", text: str = "hello") -> None:
    path.write_text(
        json.dumps({"type": "message", "chatJid": "slack:C123", "sender": sender, "text": text}),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_classify_queued_ipc_file_returns_none_when_file_disappears(tmp_path: Path):
    deps = _MessageDeps({})

    assert await classify_queued_ipc_file(tmp_path / "missing.json", tmp_path, deps) is None


@pytest.mark.asyncio
async def test_classify_queued_ipc_file_records_source_subdir_and_admin_status(tmp_path: Path):
    file_path = tmp_path / "admin" / "messages" / "message.json"
    file_path.parent.mkdir(parents=True)
    file_path.write_text("{}", encoding="utf-8")
    deps = _MessageDeps({"slack:C123": _workspace(folder="admin", is_admin=True)})

    queued = await classify_queued_ipc_file(file_path, tmp_path, deps)

    assert queued is not None
    assert queued.source_group == "admin"
    assert queued.subdir == "messages"
    assert queued.is_admin is True


@pytest.mark.asyncio
async def test_handle_message_file_broadcasts_authorized_sender_and_unlinks_file(tmp_path: Path):
    file_path = tmp_path / "message.json"
    _write_message(file_path)
    deps = _MessageDeps({"slack:C123": _workspace()})

    await handle_message_file(file_path, "project", is_admin=False, deps=deps)

    assert not file_path.exists()
    assert deps.broadcasts == [("slack:C123", deps.broadcasts[0][1])]
    assert deps.broadcasts[0][1].content == "Alice: hello"


@pytest.mark.asyncio
@pytest.mark.parametrize("is_admin", [False, True])
async def test_handle_message_file_uses_agent_fallback_for_admin_or_authorized_message(
    tmp_path: Path, is_admin: bool
):
    file_path = tmp_path / "message.json"
    _write_message(file_path, sender=None)
    deps = _MessageDeps({"slack:C123": _workspace(folder="project")})

    await handle_message_file(file_path, "project", is_admin=is_admin, deps=deps)

    assert deps.broadcasts[0][1].content == "Pynchy: hello"


@pytest.mark.asyncio
async def test_handle_message_file_blocks_wrong_source_group_but_unlinks_file(tmp_path: Path):
    file_path = tmp_path / "message.json"
    _write_message(file_path)
    deps = _MessageDeps({"slack:C123": _workspace()})

    await handle_message_file(file_path, "other", is_admin=False, deps=deps)

    assert deps.broadcasts == []
    assert not file_path.exists()


@pytest.mark.asyncio
async def test_handle_message_file_ignores_non_message_payload_but_unlinks_file(tmp_path: Path):
    file_path = tmp_path / "signal.json"
    file_path.write_text(json.dumps({"type": "other"}), encoding="utf-8")

    await handle_message_file(file_path, "project", is_admin=True, deps=_MessageDeps({}))

    assert not file_path.exists()


@pytest.mark.asyncio
async def test_handle_message_file_rejects_missing_message_dependency_contract(tmp_path: Path):
    file_path = tmp_path / "message.json"
    _write_message(file_path, sender=None)

    with pytest.raises(TypeError, match="Inbound IPC messages require IpcMessageDeps"):
        await handle_message_file(file_path, "project", is_admin=True, deps=NullIpcDeps())

    assert file_path.exists()
