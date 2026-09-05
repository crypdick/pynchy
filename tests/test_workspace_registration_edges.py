"""Public workspace-registration behavior at chat and runtime boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest
from conftest import make_settings

import pynchy.host.orchestrator.workspace_registration as registration
from pynchy.host.orchestrator.workspace_registration import (
    available_workspace_groups,
    ensure_workspace_registered,
    rebind_workspace_runtime,
    sync_workspace_profile,
)
from pynchy.workspace.api import ResolvedWorkspaceConfig, WorkspaceProfile


@dataclass(frozen=True)
class _ChatRef:
    name: str
    chat: str


@dataclass
class _Config:
    chat: str | None = None


class _Queue:
    def __init__(self, active: bool) -> None:
        self.active = active

    def has_activity(self, _runtime_id: object) -> bool:
        return self.active


class _Channel:
    def __init__(self, name: str, resolved: str | None = "discord:channel:resolved") -> None:
        self.name = name
        self.resolved = resolved
        self.created: list[str] = []

    async def resolve_chat_jid(self, _chat_name: str) -> str | None:
        return self.resolved

    async def create_group(self, name: str) -> str:
        self.created.append(name)
        return "discord:channel:created"

    def owns_jid(self, jid: str) -> bool:
        return jid.startswith("discord:channel:")


def _resolved() -> ResolvedWorkspaceConfig:
    return ResolvedWorkspaceConfig(
        skills=[],
        tools=[],
        repo=[],
        model=None,
        execution_mode="container",
        cwd=None,
        is_admin=False,
        contains_secrets=False,
    )


def _profile(jid: str, folder: str = "project") -> WorkspaceProfile:
    return WorkspaceProfile(jid=jid, name="Project", folder=folder, trigger="@pynchy")


@pytest.mark.asyncio
async def test_rebind_workspace_rejects_activity_then_removes_old_registration() -> None:
    profile = _profile("discord:channel:new")
    old = _profile("discord:channel:old")
    workspaces = {old.jid: old}

    with patch(
        "pynchy.host.orchestrator.workspace_registration.rebind_workspace_profile",
        new_callable=AsyncMock,
        return_value=None,
    ) as persist:
        with pytest.raises(RuntimeError, match="Cannot rebind active workspace"):
            await rebind_workspace_runtime(profile, workspaces, _Queue(active=True))
        persist.assert_not_awaited()

        await rebind_workspace_runtime(profile, workspaces, _Queue(active=False))
        persist.return_value = profile.jid
        await rebind_workspace_runtime(profile, {}, _Queue(active=False))

    assert workspaces == {profile.jid: profile}


@pytest.mark.asyncio
async def test_rebind_workspace_rejects_active_folder_move() -> None:
    old = _profile("discord:channel:thread", folder="parent__thread_discord-channel-thread")
    moved = _profile("discord:channel:thread", folder="scope__thread_discord-channel-thread")
    workspaces = {old.jid: old}

    with patch(
        "pynchy.host.orchestrator.workspace_registration.rebind_workspace_profile",
        new_callable=AsyncMock,
    ) as persist:
        with pytest.raises(RuntimeError, match="Cannot rebind active workspace"):
            await rebind_workspace_runtime(moved, workspaces, _Queue(active=True))
        persist.assert_not_awaited()

        await rebind_workspace_runtime(moved, workspaces, _Queue(active=False))

    assert workspaces == {moved.jid: moved}


@pytest.mark.asyncio
async def test_ensure_workspace_registered_handles_unparseable_and_unresolvable_chats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings()
    resolved = _resolved()
    register = AsyncMock()
    workspaces: dict[str, WorkspaceProfile] = {}
    folder_to_jid: dict[str, str] = {}

    monkeypatch.setattr(registration, "parse_chat_ref", lambda _ref: None)
    assert (
        await ensure_workspace_registered(
            "project",
            _Config(chat="bad-ref"),
            resolved,
            "Project",
            workspaces,
            folder_to_jid,
            [],
            settings,
            register,
        )
        is None
    )

    monkeypatch.setattr(
        registration,
        "parse_chat_ref",
        lambda _ref: _ChatRef("connection.discord.main", "project"),
    )
    assert (
        await ensure_workspace_registered(
            "project",
            _Config(chat="project"),
            resolved,
            "Project",
            workspaces,
            folder_to_jid,
            [_Channel("connection.slack.main")],
            settings,
            register,
        )
        is None
    )
    register.assert_not_awaited()

    assert (
        await ensure_workspace_registered(
            "project",
            _Config(),
            resolved,
            "Project",
            workspaces,
            folder_to_jid,
            [],
            settings,
            register,
        )
        is None
    )


@pytest.mark.asyncio
async def test_ensure_workspace_registered_keeps_existing_target() -> None:
    settings = make_settings()
    resolved = _resolved()
    profile = _profile("discord:channel:existing")
    workspaces = {profile.jid: profile}
    folder_to_jid = {"project": profile.jid}

    with patch.object(
        registration,
        "parse_chat_ref",
        return_value=_ChatRef("connection.discord.main", "project"),
    ):
        result = await ensure_workspace_registered(
            "project",
            _Config(chat="connection.discord.main.chat.project"),
            resolved,
            "Project",
            workspaces,
            folder_to_jid,
            [_Channel("connection.discord.main", resolved=profile.jid)],
            settings,
            AsyncMock(),
        )

    assert result == profile.jid


@pytest.mark.asyncio
async def test_ensure_workspace_registered_rejects_target_owned_by_another_folder() -> None:
    settings = make_settings()
    resolved = _resolved()
    old = _profile("discord:channel:old", folder="project")
    other = _profile("discord:channel:new", folder="other")
    workspaces = {old.jid: old, other.jid: other}
    folder_to_jid = {"project": old.jid}

    with patch.object(
        registration,
        "parse_chat_ref",
        return_value=_ChatRef("connection.discord.main", "project"),
    ):
        result = await ensure_workspace_registered(
            "project",
            _Config(chat="connection.discord.main.chat.project"),
            resolved,
            "Project",
            workspaces,
            folder_to_jid,
            [_Channel("connection.discord.main", resolved=other.jid)],
            settings,
            AsyncMock(),
        )

    assert result == old.jid
    assert folder_to_jid["project"] == old.jid


@pytest.mark.asyncio
async def test_ensure_workspace_registered_requires_rebind_for_target_change() -> None:
    settings = make_settings()
    resolved = _resolved()
    old = _profile("discord:channel:old")
    workspaces = {old.jid: old}
    folder_to_jid = {"project": old.jid}

    with patch.object(
        registration,
        "parse_chat_ref",
        return_value=_ChatRef("connection.discord.main", "project"),
    ):
        result = await ensure_workspace_registered(
            "project",
            _Config(chat="connection.discord.main.chat.project"),
            resolved,
            "Project",
            workspaces,
            folder_to_jid,
            [_Channel("connection.discord.main", resolved="discord:channel:new")],
            settings,
            AsyncMock(),
        )

    assert result == old.jid
    assert folder_to_jid["project"] == old.jid


def test_available_workspace_groups_filters_sync_and_unowned_chats() -> None:
    channel = _Channel("connection.discord.main")
    visible = available_workspace_groups(
        [
            {"jid": "__group_sync__", "name": "sync", "last_message_time": None},
            {"jid": "discord:channel:owned", "name": "Owned", "last_message_time": "now"},
            {"jid": "slack:C1", "name": "Slack", "last_message_time": "later"},
        ],
        {"discord:channel:owned": _profile("discord:channel:owned")},
        [channel],
    )

    assert visible == [
        {
            "jid": "discord:channel:owned",
            "name": "Owned",
            "lastActivity": "now",
            "isRegistered": True,
        }
    ]


@pytest.mark.asyncio
async def test_sync_workspace_profile_ignores_missing_registration() -> None:
    with patch(
        "pynchy.host.orchestrator.workspace_registration.set_workspace_profile",
        new_callable=AsyncMock,
    ) as persist:
        await sync_workspace_profile(
            None,
            {},
            "project",
            "Project",
            _resolved(),
        )
        await sync_workspace_profile(
            "discord:channel:missing",
            {},
            "project",
            "Project",
            _resolved(),
        )

    persist.assert_not_awaited()
