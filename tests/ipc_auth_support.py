"""Tests for IPC authorization and task scheduling."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from conftest import NullIpcDeps, init_test_database, make_settings

from pynchy.state import (
    set_workspace_profile,
)
from pynchy.workspace.api import WorkspaceProfile

ADMIN_GROUP = WorkspaceProfile(
    jid="admin-1@g.us",
    name="Admin",
    folder="admin-1",
    trigger="always",
    added_at="2024-01-01T00:00:00.000Z",
    is_admin=True,
)

OTHER_GROUP = WorkspaceProfile(
    jid="other@g.us",
    name="Other",
    folder="other-group",
    trigger="@pynchy",
    added_at="2024-01-01T00:00:00.000Z",
)

THIRD_GROUP = WorkspaceProfile(
    jid="third@g.us",
    name="Third",
    folder="third-group",
    trigger="@pynchy",
    added_at="2024-01-01T00:00:00.000Z",
)


def _test_settings(*, data_dir=None):
    return make_settings(**({"data_dir": data_dir} if data_dir is not None else {}))


class MockDeps(NullIpcDeps):
    """Mock IPC dependencies."""

    def __init__(self, groups: dict[str, WorkspaceProfile]):
        self._groups = groups
        self.broadcast_messages: list[tuple[str, str]] = []
        self.host_messages: list[tuple[str, str]] = []
        self.system_notices: list[tuple[str, str]] = []
        self.cleared_sessions: list[str] = []
        self.cleared_chats: list[str] = []
        self.enqueued_checks: list[str] = []
        self.requested_deploys: list[tuple[str | None, str, bool, str]] = []

    async def broadcast_to_channels(self, jid: str, event: object) -> None:
        content = event.content if hasattr(event, "content") else str(event)
        self.broadcast_messages.append((jid, content))

    async def broadcast_host_message(self, jid: str, text: str) -> None:
        self.host_messages.append((jid, text))

    async def broadcast_system_notice(self, jid: str, text: str) -> None:
        self.system_notices.append((jid, text))

    async def send_message(self, jid: str, text: str) -> None:
        pass

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return self._groups

    def register_workspace(self, profile: WorkspaceProfile) -> None:
        # Only update the in-memory dict — no DB write needed.
        # The real adapter fires a background task for the DB, but in tests
        # we check deps.workspaces() (the dict), not the DB.  Avoiding the
        # fire-and-forget future prevents aiosqlite's worker thread from
        # racing against pytest's per-test event-loop teardown.
        self._groups[profile.jid] = profile

    async def clear_session(self, group_folder: str) -> None:
        self.cleared_sessions.append(group_folder)

    async def clear_chat_history(self, chat_jid: str) -> None:
        self.cleared_chats.append(chat_jid)

    def enqueue_message_check(self, group_jid: str) -> None:
        self.enqueued_checks.append(group_jid)

    async def request_deploy(
        self,
        *,
        chat_jid: str | None,
        commit_sha: str,
        rebuild: bool,
        resume_prompt: str,
    ) -> None:
        self.requested_deploys.append((chat_jid, commit_sha, rebuild, resume_prompt))


@pytest.fixture
async def deps(monkeypatch):
    await init_test_database()
    monkeypatch.setattr(
        "pynchy.host.orchestrator.terminal_task_retirement.cancel_scheduled_agent_workflow",
        AsyncMock(return_value=False),
    )

    groups = {
        "admin-1@g.us": ADMIN_GROUP,
        "other@g.us": OTHER_GROUP,
        "third@g.us": THIRD_GROUP,
    }

    await set_workspace_profile(ADMIN_GROUP)
    await set_workspace_profile(OTHER_GROUP)
    await set_workspace_profile(THIRD_GROUP)

    return MockDeps(groups)
