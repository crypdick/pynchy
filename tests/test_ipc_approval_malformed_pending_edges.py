"""Fail-closed handling for malformed persisted approval payloads."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from conftest import NullIpcDeps, make_settings

from pynchy.host.container_manager.ipc.handlers_approval import process_approval_decision
from tests.approval_support import write_encrypted_pending_approval

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def settings(tmp_path: Path):
    return make_settings(data_dir=tmp_path)


def _write_pending(ipc_dir: Path) -> Path:
    approvals_root = ipc_dir.parent / "approvals"
    path, _ = write_encrypted_pending_approval(
        approvals_root,
        request_id="invalid-pending",
        tool_name="my_tool",
        source_group="grp",
        approval_chat_jid="j@g.us",
        request_data={"type": "service:my_tool", "request_id": "invalid-pending"},
    )
    return path


def _write_decision(ipc_dir: Path) -> Path:
    decisions_dir = ipc_dir.parent / "approvals" / "grp" / "approval_decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    pending = json.loads(
        (
            ipc_dir.parent / "approvals" / "grp" / "pending_approvals" / "invalid-pending.json"
        ).read_text(encoding="utf-8")
    )
    decision_file = decisions_dir / "invalid-pending.json"
    decision_file.write_text(
        json.dumps(
            {
                "request_id": "invalid-pending",
                "guarded_action_id": pending["guarded_action_id"],
                "request_payload_hash": pending["request_payload_hash"],
                "source_group": "grp",
                "approved": True,
                "decided_by": "testuser",
                "decided_at": "2026-07-31T12:01:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    return decision_file


@pytest.mark.asyncio
async def test_non_mapping_pending_payload_is_rejected_and_cleaned(
    tmp_path: Path, settings
) -> None:
    ipc_dir = tmp_path / "ipc"
    ipc_dir.mkdir()
    pending_file = _write_pending(ipc_dir)
    decision_file = _write_decision(ipc_dir)

    with (
        patch(
            "pynchy.host.container_manager.ipc.handlers_approval.get_settings",
            return_value=settings,
        ),
        patch(
            "pynchy.host.container_manager.security.approval.read_pending_approval",
            return_value=[],
        ),
    ):
        await process_approval_decision(decision_file, "grp", deps=NullIpcDeps())

    assert not pending_file.exists()
    assert not decision_file.exists()
