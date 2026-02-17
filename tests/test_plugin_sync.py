from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pynchy.config import PluginConfig
from pynchy.plugin.sync import normalize_repo_url, sync_configured_plugins


def test_normalize_repo_url_expands_github_shorthand() -> None:
    assert normalize_repo_url("crypdick/pynchy-plugin-whatsapp") == (
        "https://github.com/crypdick/pynchy-plugin-whatsapp.git"
    )


def test_normalize_repo_url_keeps_explicit_url() -> None:
    url = "https://github.com/crypdick/pynchy-plugin-whatsapp.git"
    assert normalize_repo_url(url) == url


def test_sync_configured_plugins_skips_disabled(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        plugins_dir=tmp_path / "plugins",
        plugins={
            "enabled": PluginConfig(
                repo="crypdick/pynchy-plugin-whatsapp",
                ref="main",
                enabled=True,
            ),
            "disabled": PluginConfig(
                repo="crypdick/disabled",
                ref="main",
                enabled=False,
            ),
        },
    )

    enabled_dir = settings.plugins_dir / "enabled"
    with (
        patch("pynchy.plugin.sync.get_settings", return_value=settings),
        patch("pynchy.plugin.sync._sync_single_plugin", return_value=enabled_dir) as sync_one,
        patch("pynchy.plugin.sync._plugin_revision", return_value="abc123"),
        patch("pynchy.plugin.sync._load_install_state", return_value={}),
        patch("pynchy.plugin.sync._save_install_state"),
        patch("pynchy.plugin.sync._install_plugin_in_host_env") as install_one,
        patch("pynchy.plugin.sync.logger.info") as log_info,
    ):
        result = sync_configured_plugins()

    assert result == {"enabled": enabled_dir}
    sync_one.assert_called_once()
    install_one.assert_called_once_with("enabled", enabled_dir)
    assert any(
        call.args and call.args[0] == "Plugin revision discovered"
        for call in log_info.call_args_list
    )


def test_sync_configured_plugins_skips_reinstall_for_same_revision(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        plugins_dir=tmp_path / "plugins",
        plugins={
            "enabled": PluginConfig(
                repo="crypdick/pynchy-plugin-whatsapp",
                ref="main",
                enabled=True,
            ),
        },
    )
    enabled_dir = settings.plugins_dir / "enabled"
    with (
        patch("pynchy.plugin.sync.get_settings", return_value=settings),
        patch("pynchy.plugin.sync._sync_single_plugin", return_value=enabled_dir),
        patch("pynchy.plugin.sync._plugin_revision", return_value="abc123"),
        patch(
            "pynchy.plugin.sync._load_install_state",
            return_value={"enabled": {"revision": "abc123"}},
        ),
        patch("pynchy.plugin.sync._save_install_state") as save_state,
        patch("pynchy.plugin.sync._install_plugin_in_host_env") as install_one,
        patch("pynchy.plugin.sync._is_plugin_importable", return_value=True),
        patch("pynchy.plugin.sync.logger.info") as log_info,
    ):
        result = sync_configured_plugins()

    assert result == {"enabled": enabled_dir}
    install_one.assert_not_called()
    save_state.assert_not_called()
    assert any(
        call.args and call.args[0] == "Plugin already up to date"
        for call in log_info.call_args_list
    )
