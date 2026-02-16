"""End-to-end integration test for first-party plugins.

This test intentionally exercises the real startup path:
1. clone/fetch plugin repos from GitHub
2. install plugins into the current host Python environment
3. load plugins through the real plugin manager
4. validate concrete functionality for both first-party plugins
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pynchy.app import PynchyApp
from pynchy.channel_runtime import (
    ChannelPluginContext,
    load_channels,
    resolve_default_channel,
)
from pynchy.config import (
    AgentConfig,
    ChannelsConfig,
    CommandWordsConfig,
    ContainerConfig,
    IntervalsConfig,
    LoggingConfig,
    PluginConfig,
    QueueConfig,
    SchedulerConfig,
    SecretsConfig,
    SecurityConfig,
    ServerConfig,
    Settings,
    WorkspaceDefaultsConfig,
)
from pynchy.db import _init_test_database, store_message
from pynchy.plugin import get_plugin_manager
from pynchy.plugin_sync import sync_configured_plugins
from pynchy.types import NewMessage, RegisteredGroup
from pynchy.workspace_config import (
    configure_plugin_workspaces,
    load_workspace_config,
    reconcile_workspaces,
)


def _integration_settings(tmp_path: Path) -> Settings:
    settings = Settings.model_construct(
        agent=AgentConfig(),
        container=ContainerConfig(),
        server=ServerConfig(),
        logging=LoggingConfig(),
        secrets=SecretsConfig(),
        workspace_defaults=WorkspaceDefaultsConfig(),
        workspaces={},
        commands=CommandWordsConfig(),
        scheduler=SchedulerConfig(),
        intervals=IntervalsConfig(),
        queue=QueueConfig(),
        channels=ChannelsConfig(default="whatsapp"),
        plugins={
            "whatsapp": PluginConfig(
                repo="crypdick/pynchy-plugin-whatsapp",
                ref="main",
                enabled=True,
                trusted=True,
            ),
            "code-improver": PluginConfig(
                repo="crypdick/pynchy-plugin-code-improver",
                ref="main",
                enabled=True,
                trusted=True,
            ),
        },
        security=SecurityConfig(),
    )
    settings.__dict__["plugins_dir"] = tmp_path / "plugins"
    settings.__dict__["groups_dir"] = tmp_path / "groups"
    settings.__dict__["store_dir"] = tmp_path / "store"
    settings.__dict__["home_dir"] = tmp_path
    return settings


class _FakeProvisioningChannel:
    def __init__(self) -> None:
        self.created_groups: list[str] = []

    async def create_group(self, name: str) -> str:
        self.created_groups.append(name)
        return "code-improver@g.us"


class _FakeStdin:
    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data += data

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self._returncode: int | None = None
        self._done = asyncio.Event()
        self.pid = 4242

    async def emit_result(self, payload: dict[str, object]) -> None:
        marker = (
            f"{Settings.OUTPUT_START_MARKER}\n{json.dumps(payload)}\n{Settings.OUTPUT_END_MARKER}\n"
        )
        await asyncio.sleep(0.01)
        self.stdout.feed_data(marker.encode())
        await asyncio.sleep(0.01)
        self._returncode = 0
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._done.set()

    async def wait(self) -> int:
        await self._done.wait()
        return self._returncode or 0

    def kill(self) -> None:
        self._returncode = -9
        self._done.set()


@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_first_party_plugins_sync_install_and_functionality(tmp_path: Path) -> None:
    """Validate first-party plugins via real clone + install + hook behavior."""
    settings = _integration_settings(tmp_path)

    with patch("pynchy.plugin_sync.get_settings", return_value=settings):
        synced = sync_configured_plugins()
    assert set(synced) == {"whatsapp", "code-improver"}
    assert (settings.plugins_dir / "whatsapp").exists()
    assert (settings.plugins_dir / "code-improver").exists()

    install_state_path = settings.plugins_dir / ".host-install-state.json"
    assert install_state_path.exists()
    install_state = json.loads(install_state_path.read_text())
    assert "whatsapp" in install_state
    assert "code-improver" in install_state

    with (
        patch("pynchy.plugin.get_settings", return_value=settings),
        patch("pynchy.channel_runtime.get_settings", return_value=settings),
        patch("pynchy.workspace_config.get_settings", return_value=settings),
    ):
        pm = get_plugin_manager()

        # WhatsApp plugin functionality: it must expose a channel plugin and wire
        # context callbacks into channel construction.
        whatsapp_entry = importlib.import_module("pynchy_plugin_whatsapp")
        captured: dict[str, object] = {}

        class FakeWhatsAppChannel:
            name = "whatsapp"

            def __init__(self, on_message, on_chat_metadata, registered_groups) -> None:
                captured["on_message"] = on_message
                captured["on_chat_metadata"] = on_chat_metadata
                captured["registered_groups"] = registered_groups

        with patch.object(whatsapp_entry, "WhatsAppChannel", FakeWhatsAppChannel):
            context = ChannelPluginContext(
                on_message_callback=lambda chat_jid, message: None,
                on_chat_metadata_callback=lambda chat_jid, timestamp, name: None,
                registered_groups=lambda: {},
                send_message=lambda jid, text: None,
            )
            channels = load_channels(pm, context)

        assert len(channels) == 1
        assert getattr(channels[0], "name", None) == "whatsapp"
        assert resolve_default_channel(channels) is channels[0]
        assert callable(captured.get("on_message"))
        assert callable(captured.get("on_chat_metadata"))
        assert callable(captured.get("registered_groups"))

        # Code improver plugin functionality: workspace spec should be loaded and
        # the bundled CLAUDE.md should be seeded when reconciling workspaces.
        specs = pm.hook.pynchy_workspace_spec()
        code_improver = next(
            (spec for spec in specs if spec.get("folder") == "code-improver"), None
        )
        assert code_improver is not None
        assert code_improver["config"]["project_access"] is True
        assert code_improver["config"]["context_mode"] == "isolated"

        configure_plugin_workspaces(pm)
        cfg = load_workspace_config("code-improver")
        assert cfg is not None
        assert cfg.project_access is True
        assert cfg.context_mode == "isolated"

        channel = _FakeProvisioningChannel()
        registered: dict[str, object] = {}

        async def register_fn(jid: str, group) -> None:
            registered[jid] = group

        await reconcile_workspaces(
            registered_groups={},
            channels=[channel],
            register_fn=register_fn,
        )

    assert channel.created_groups == ["Code Improver"]
    assert "code-improver@g.us" in registered
    group = registered["code-improver@g.us"]
    assert getattr(group, "folder", None) == "code-improver"

    claude_md_path = settings.groups_dir / "code-improver" / "CLAUDE.md"
    assert claude_md_path.exists()
    assert "Pynchy Core Code Improver" in claude_md_path.read_text()


@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_first_party_plugins_app_flow_with_llm_boundary_mocked(tmp_path: Path) -> None:
    """Validate app message flow with first-party plugins while stubbing LLM calls."""
    settings = _integration_settings(tmp_path)

    with patch("pynchy.plugin_sync.get_settings", return_value=settings):
        sync_configured_plugins()

    await _init_test_database()
    message = NewMessage(
        id="m-e2e-1",
        chat_jid="group@g.us",
        sender="user@s.whatsapp.net",
        sender_name="Alice",
        content="@pynchy run a plugin-backed integration check",
        timestamp="2026-02-15T10:00:00.000Z",
    )
    await store_message(message)

    with (
        patch("pynchy.plugin.get_settings", return_value=settings),
        patch("pynchy.channel_runtime.get_settings", return_value=settings),
        patch("pynchy.workspace_config.get_settings", return_value=settings),
        patch("pynchy.message_handler.get_settings", return_value=settings),
        patch("pynchy.output_handler.get_settings", return_value=settings),
        patch("pynchy.container_runner._credentials.get_settings", return_value=settings),
        patch("pynchy.container_runner._mounts.get_settings", return_value=settings),
        patch("pynchy.container_runner._session_prep.get_settings", return_value=settings),
        patch("pynchy.container_runner._orchestrator.get_settings", return_value=settings),
        patch("pynchy.container_runner._snapshots.get_settings", return_value=settings),
        patch("pynchy.app.get_settings", return_value=settings),
    ):
        pm = get_plugin_manager()

        whatsapp_entry = importlib.import_module("pynchy_plugin_whatsapp")

        class FakeWhatsAppChannel:
            name = "whatsapp"
            prefix_assistant_name = True

            def __init__(self, on_message, on_chat_metadata, registered_groups) -> None:
                self._on_message = on_message
                self._on_chat_metadata = on_chat_metadata
                self._registered_groups = registered_groups
                self.sent_messages: list[tuple[str, str]] = []
                self.sent_reactions: list[tuple[str, str, str, str]] = []
                self.typing_events: list[tuple[str, bool]] = []

            def is_connected(self) -> bool:
                return True

            def owns_jid(self, jid: str) -> bool:
                return jid.endswith("@g.us") or jid.endswith("@s.whatsapp.net")

            async def send_message(self, jid: str, text: str) -> None:
                self.sent_messages.append((jid, text))

            async def send_reaction(
                self, chat_jid: str, message_id: str, sender_jid: str, emoji: str
            ) -> None:
                self.sent_reactions.append((chat_jid, message_id, sender_jid, emoji))

            async def set_typing(self, jid: str, is_typing: bool) -> None:
                self.typing_events.append((jid, is_typing))

            async def disconnect(self) -> None:
                return

        with patch.object(whatsapp_entry, "WhatsAppChannel", FakeWhatsAppChannel):
            context = ChannelPluginContext(
                on_message_callback=lambda chat_jid, msg: None,
                on_chat_metadata_callback=lambda chat_jid, ts, name=None: None,
                registered_groups=lambda: {},
                send_message=lambda jid, text: None,
            )
            channels = load_channels(pm, context)

        app = PynchyApp()
        app.plugin_manager = pm
        app.channels = channels
        app.registered_groups = {
            "group@g.us": RegisteredGroup(
                name="Plugin E2E",
                folder="plugin-e2e",
                trigger="@pynchy",
                added_at="2026-02-15T09:59:00.000Z",
            )
        }

        fake_proc = _FakeProcess()
        driver = asyncio.create_task(
            fake_proc.emit_result(
                {
                    "status": "success",
                    "type": "result",
                    "result": "Plugin-backed run completed",
                    "new_session_id": "sess-plugin-e2e",
                }
            )
        )
        create_calls: list[tuple[str, ...]] = []

        async def fake_create_subprocess_exec(*args, **kwargs):
            create_calls.append(tuple(str(a) for a in args))
            return fake_proc

        with (
            patch(
                "pynchy.container_runner._orchestrator.asyncio.create_subprocess_exec",
                fake_create_subprocess_exec,
            ),
            patch("pynchy.container_runner._mounts._write_env_file", return_value=None),
            patch(
                "pynchy.container_runner._orchestrator.get_runtime",
                return_value=SimpleNamespace(cli="container"),
            ),
            patch("pynchy.container_runner._mounts._sync_skills", return_value=None),
        ):
            (settings.groups_dir / "plugin-e2e").mkdir(parents=True, exist_ok=True)
            processed = await app._process_group_messages("group@g.us")

        await driver

    channel = channels[0]
    assert processed is True
    assert create_calls, "Expected container subprocess to be invoked"
    assert app.sessions.get("plugin-e2e") == "sess-plugin-e2e"
    assert fake_proc.stdin.closed is True
    assert b"agent_core_module" in fake_proc.stdin.data
    assert b"Plugin-backed run completed" not in fake_proc.stdin.data
    assert any("Plugin-backed run completed" in text for _, text in channel.sent_messages)
    assert channel.sent_reactions == [("group@g.us", "m-e2e-1", "user@s.whatsapp.net", "👀")]
    assert channel.typing_events == [("group@g.us", True), ("group@g.us", False)]
