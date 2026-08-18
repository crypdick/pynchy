"""Behavioral tests for host-owned plugin composition."""

from __future__ import annotations

from unittest.mock import MagicMock

import pluggy
import pytest
from conftest import make_settings

from pynchy.config.api import (
    CalDAVServerConfig,
    CalDAVTool,
    PluginConfig,
)
from pynchy.host.orchestrator import plugin_configuration
from pynchy.plugins.integrations.computer_use import ComputerUsePlugin
from pynchy.plugins.integrations.cua_driver import CuaDriverComputerUsePlugin
from pynchy.plugins.integrations.desktop_screenshot import DesktopScreenshotPlugin
from pynchy.plugins.integrations.google_setup import GoogleSetupPlugin
from pynchy.plugins.integrations.marketplace_health import MarketplaceHealthPlugin
from pynchy.plugins.integrations.peekaboo import PeekabooComputerUsePlugin
from pynchy.plugins.integrations.ssh_x11 import SshX11ComputerUsePlugin

CALDAV_ENV_NAME = "CALDAV_PASSWORD"


def _plugin_manager(*plugins: tuple[str, object]) -> pluggy.PluginManager:
    manager = pluggy.PluginManager("pynchy")
    for name, plugin in plugins:
        manager.register(plugin, name=name)
    return manager


def test_configure_computer_use_plugins_applies_provider_options() -> None:
    router = ComputerUsePlugin()
    cua = CuaDriverComputerUsePlugin()
    peekaboo = PeekabooComputerUsePlugin()
    ssh_x11 = SshX11ComputerUsePlugin()
    manager = _plugin_manager(
        ("builtin-computer-use", router),
        ("builtin-cua-driver", cua),
        ("builtin-peekaboo", peekaboo),
        ("builtin-ssh-x11", ssh_x11),
    )
    settings = make_settings(
        plugins={
            "computer-use": PluginConfig(options={"providers": ["cua-driver"]}),
            "cua-driver": PluginConfig(options={"binary": "/opt/cua", "timeout_seconds": 12}),
            "peekaboo": PluginConfig(options={"binary": "/opt/peekaboo", "timeout_seconds": 8}),
            "ssh-x11": PluginConfig(
                options={
                    "host": "desktop",
                    "user": "operator",
                    "private_key": "/run/secrets/x11/key",
                    "known_hosts": "/run/secrets/x11/known_hosts",
                }
            ),
        },
    )

    plugin_configuration.configure_computer_use_plugins(manager, settings)

    assert cua.pynchy_computer_use_backend().config.binary == "/opt/cua"
    assert cua.pynchy_computer_use_backend().config.timeout_seconds == 12
    assert peekaboo.pynchy_computer_use_backend().config.binary == "/opt/peekaboo"
    assert ssh_x11.pynchy_computer_use_backend().config.host == "desktop"
    assert router.pynchy_service_handler(computer_use_backends=()).actions


def test_configure_desktop_screenshot_plugin_injects_gateway_accessor(
    monkeypatch, tmp_path
) -> None:
    plugin = DesktopScreenshotPlugin()
    manager = _plugin_manager(("builtin-desktop-screenshot", plugin))
    settings = make_settings(data_dir=tmp_path / "data")
    gateway = MagicMock(port=4000, key="gateway-key")
    configure = MagicMock(wraps=plugin.configure)
    plugin.configure = configure
    monkeypatch.setattr(plugin_configuration.gateway_manager, "get_gateway", lambda: gateway)

    plugin_configuration.configure_desktop_screenshot_plugin(manager, settings)

    runtime = configure.call_args.args[0]
    assert runtime.data_dir == settings.data_dir
    assert runtime.default_model in {settings.agent.model, "gpt-5.5"}
    assert runtime.vision_gateway().port == 4000
    assert runtime.vision_gateway().api_key == "gateway-key"  # pragma: allowlist secret
    monkeypatch.setattr(plugin_configuration.gateway_manager, "get_gateway", lambda: None)
    assert runtime.vision_gateway() is None


def test_configure_desktop_screenshot_plugin_ignores_unregistered_plugin(tmp_path) -> None:
    plugin_configuration.configure_desktop_screenshot_plugin(
        _plugin_manager(), make_settings(data_dir=tmp_path / "data")
    )


@pytest.mark.parametrize("configured", [False, True])
def test_configure_caldav_plugin_resolves_server_options(monkeypatch, configured: bool) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        plugin_configuration,
        "configure_caldav_runtime",
        lambda runtime: captured.setdefault("runtime", runtime),
    )
    tools = {}
    if configured:
        tools["caldav"] = CalDAVTool(
            type="caldav",
            default_server="work",
            servers={
                "work": CalDAVServerConfig(
                    url="https://calendar.example.test/dav",
                    username="user@example.test",
                    password_env=CALDAV_ENV_NAME,
                    allow=["work"],
                    ignore=["private"],
                )
            },
        )
    settings = make_settings(tools=tools)

    plugin_configuration.configure_caldav_plugin(settings)

    runtime = captured["runtime"]
    if not configured:
        assert not runtime.default_server
        assert runtime.servers == {}
    else:
        server = runtime.servers["work"]
        assert runtime.default_server == "work"
        assert server.url == "https://calendar.example.test/dav"
        assert server.password_env == CALDAV_ENV_NAME
        assert server.allow == ("work",)
        assert server.ignore == ("private",)


@pytest.mark.parametrize("configured", [False, True])
def test_configure_gog_plugin_resolves_paths_and_workspace_policy(
    monkeypatch, tmp_path, configured: bool
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        plugin_configuration,
        "configure_gog_runtime",
        lambda runtime: captured.setdefault("runtime", runtime),
    )
    settings = make_settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        plugins=(
            {
                "gog": PluginConfig(
                    options={
                        "home": "state/gog",
                        "oauth_client_path": "oauth/client.json",
                    }
                )
            }
            if configured
            else {}
        ),
    )

    plugin_configuration.configure_gog_plugin(settings)

    runtime = captured["runtime"]
    assert runtime.home == (tmp_path / "state/gog" if configured else tmp_path / "data/gog")
    assert runtime.oauth_client_path == (tmp_path / "oauth/client.json" if configured else None)
    assert runtime.workspace_enables_gog("missing") is False


def test_configure_marketplace_health_plugin_injects_projection_and_reader_policy(
    monkeypatch, tmp_path
) -> None:
    plugin = MarketplaceHealthPlugin()
    configure = MagicMock(wraps=plugin.configure)
    plugin.configure = configure
    manager = _plugin_manager(("marketplace-health", plugin))
    settings = make_settings(
        data_dir=tmp_path / "data",
        plugins={
            "marketplace-health": PluginConfig(
                options={"pending_actions_file": str(tmp_path / "pending.json")}
            )
        },
    )
    configured: dict[str, object] = {}
    monkeypatch.setattr(
        plugin_configuration,
        "configure_marketplace_health_runtime",
        lambda runtime: configured.setdefault("runtime", runtime),
    )

    plugin_configuration.configure_marketplace_health_plugin(manager, settings)

    runtime = configured["runtime"]
    assert runtime.options.pending_actions_file == tmp_path / "pending.json"
    assert runtime.reader_environment("unknown") is None
    plugin.configure.assert_called_once_with(runtime)


def test_configure_google_setup_plugin_injects_profiles_and_runtime_policy(
    monkeypatch, tmp_path
) -> None:
    plugin = GoogleSetupPlugin()
    configure = MagicMock(wraps=plugin.configure)
    plugin.configure = configure
    manager = _plugin_manager(("builtin-google-setup", plugin))
    settings = make_settings(
        data_dir=tmp_path / "data",
        chrome_profiles=["personal", "work"],
    )
    configured: dict[str, object] = {}
    monkeypatch.setattr(
        plugin_configuration,
        "configure_google_setup_runtime",
        lambda runtime: configured.setdefault("runtime", runtime),
    )

    plugin_configuration.configure_google_setup_plugin(manager, settings)

    runtime = configured["runtime"]
    assert runtime.data_dir == tmp_path / "data"
    assert runtime.chrome_profiles == frozenset({"personal", "work"})
    assert runtime.workspace_tools("missing") is None
    assert runtime.workspace_is_admin("missing") is False
    assert configure.call_args.args == (("personal", "work"),)


def test_configure_linear_plugin_wires_host_runtime_contracts() -> None:
    manager = _plugin_manager()
    settings = make_settings()

    plugin_configuration.configure_linear_plugin(manager, settings, lambda: None)

    assert plugin_configuration.configured_linear_accounts() == ()
