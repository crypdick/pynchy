"""Tests for dynamic thread workspaces inheriting parent workspace profiles."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from conftest import (
    NullChannel,
    init_test_database,
    make_container_runtime_operations,
    make_settings,
)

from pynchy.config.api import ProfileConfig, WorkspaceConfig
from pynchy.conversation.api import dynamic_thread_folder
from pynchy.host.orchestrator import session_handler
from pynchy.host.orchestrator.concurrency import GroupQueue
from pynchy.host.orchestrator.workspace_config import load_resolved_config
from pynchy.plugins.api import NewMessage
from pynchy.workspace.api import WorkspaceProfile


class _DiscordChannel(NullChannel):
    name = "connection.discord.main"

    def owns_jid(self, jid) -> bool:
        return str(jid).startswith("discord:")


class _Deps:
    def __init__(self):
        self.sessions = {}
        self.session_cleared = set()
        self.last_agent_timestamp = {}
        self.queue = GroupQueue(
            10,
            make_container_runtime_operations(),
        )
        self.channels = [_DiscordChannel()]
        self.workspaces = {
            "discord:channel:parent": WorkspaceProfile(
                jid="discord:channel:parent",
                name="Admin",
                folder="admin",
                trigger="@Pynchy",
                is_admin=True,
                added_at=datetime.now(UTC).isoformat(),
            )
        }
        self.registered = []
        self.emitted = []

    async def register_workspace(self, profile: WorkspaceProfile) -> None:
        self.workspaces[profile.jid] = profile
        self.registered.append(profile)

    async def save_state(self) -> None: ...

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...

    def emit(self, event) -> None:
        self.emitted.append(event)

    def current_deploy_revision(self) -> tuple[str, str]:
        return ("test", "test")


@pytest.fixture
async def db():
    await init_test_database()


async def test_unknown_discord_thread_registers_inherited_workspace(db, monkeypatch, tmp_path):
    settings = make_settings(
        groups_dir=tmp_path / "groups",
        profiles={"admin": ProfileConfig(is_admin=True)},
        workspaces={"admin": WorkspaceConfig(profiles=["admin"])},
    )
    monkeypatch.setattr("pynchy.host.orchestrator.workspace_config.get_settings", lambda: settings)
    deps = _Deps()
    msg = NewMessage(
        id="discord-msg-1",
        chat_jid="discord:channel:thread",
        sender="42",
        sender_name="Alice",
        content="start",
        timestamp=datetime.now(UTC).isoformat(),
        metadata={
            "discord_parent_chat_jid": "discord:channel:parent",
            "discord_channel_name": "thread-1",
        },
    )

    await session_handler.on_inbound(deps, msg.chat_jid, msg)

    assert len(deps.registered) == 1
    child = deps.registered[0]
    assert child.jid == "discord:channel:thread"
    assert child.folder == dynamic_thread_folder("admin", "discord:channel:thread")
    assert child.name == "Admin/thread-1"
    assert child.is_admin is True


@pytest.mark.parametrize(
    "metadata",
    [
        {"discord_parent_chat_jid": None},
        {"discord_parent_chat_jid": "discord:channel:missing"},
    ],
)
async def test_unknown_thread_without_known_parent_is_not_registered(db, metadata):
    deps = _Deps()
    msg = NewMessage(
        id="discord-msg-unregistered",
        chat_jid="discord:channel:thread",
        sender="42",
        sender_name="Alice",
        content="start",
        timestamp=datetime.now(UTC).isoformat(),
        metadata=metadata,
    )

    await session_handler.on_inbound(deps, msg.chat_jid, msg)

    assert deps.registered == []


async def test_unknown_thread_with_blank_name_uses_jid(db):
    deps = _Deps()
    msg = NewMessage(
        id="discord-msg-blank-name",
        chat_jid="discord:channel:thread",
        sender="42",
        sender_name="Alice",
        content="start",
        timestamp=datetime.now(UTC).isoformat(),
        metadata={
            "discord_parent_chat_jid": "discord:channel:parent",
            "discord_channel_name": "  ",
        },
    )

    await session_handler.on_inbound(deps, msg.chat_jid, msg)

    assert deps.registered[0].name == "Admin/discord:channel:thread"


def test_dynamic_thread_folder_resolves_parent_workspace_config(monkeypatch):
    settings = make_settings(
        profiles={"admin": ProfileConfig(repo="crypdick/pynchy")},
        workspaces={
            "admin": WorkspaceConfig(
                profiles=["admin"],
                model="chatgpt/gpt-5.3-codex-spark",
            )
        },
    )
    monkeypatch.setattr("pynchy.host.orchestrator.workspace_config.get_settings", lambda: settings)

    resolved = load_resolved_config(dynamic_thread_folder("admin", "discord:channel:thread"))

    assert resolved is not None
    assert resolved.repo == ["crypdick/pynchy"]
    assert resolved.model == "chatgpt/gpt-5.3-codex-spark"


def test_dynamic_thread_always_uses_parent_profile(monkeypatch):
    child_folder = dynamic_thread_folder("admin", "discord:channel:thread")
    settings = make_settings(
        profiles={
            "admin": ProfileConfig(repo="crypdick/pynchy", skills=["ops"]),
            "child-override": ProfileConfig(repo="example/incorrect", skills=["core"]),
        },
        workspaces={
            "admin": WorkspaceConfig(
                profiles=["admin"],
                model="chatgpt/gpt-5.3-codex-spark",
            ),
            child_folder: WorkspaceConfig(
                profiles=["child-override"],
                model="chatgpt/gpt-5.3-codex-mini",
            ),
        },
    )
    monkeypatch.setattr("pynchy.host.orchestrator.workspace_config.get_settings", lambda: settings)

    resolved = settings.resolved_workspace_config(child_folder)
    loaded = load_resolved_config(child_folder)

    assert resolved is not None
    assert resolved.repo == ["crypdick/pynchy"]
    assert resolved.skills == ["ops"]
    assert resolved.model == "chatgpt/gpt-5.3-codex-spark"
    assert loaded == resolved
