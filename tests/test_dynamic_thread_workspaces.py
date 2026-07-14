"""Tests for dynamic thread workspaces inheriting parent workspace profiles."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from conftest import NullChannel, init_test_database, make_settings

from pynchy.config.models import ProfileConfig, WorkspaceConfig
from pynchy.host.orchestrator import session_handler
from pynchy.host.orchestrator.concurrency import GroupQueue
from pynchy.host.orchestrator.workspace_config import dynamic_thread_folder, load_resolved_config
from pynchy.types import NewMessage, WorkspaceProfile


class _DiscordChannel(NullChannel):
    name = "connection.discord.main"

    def owns_jid(self, jid) -> bool:
        return str(jid).startswith("discord:")


class _Deps:
    def __init__(self):
        self.sessions = {}
        self.session_cleared = set()
        self.last_agent_timestamp = {}
        self.queue = GroupQueue()
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
