from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pynchy.config import PluginConfig
from pynchy.plugin_sync import normalize_repo_url, sync_configured_plugins


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
        patch("pynchy.plugin_sync.get_settings", return_value=settings),
        patch("pynchy.plugin_sync._sync_single_plugin", return_value=enabled_dir) as sync_one,
    ):
        result = sync_configured_plugins()

    assert result == {"enabled": enabled_dir}
    sync_one.assert_called_once()
