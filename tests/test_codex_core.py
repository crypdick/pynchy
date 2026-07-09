"""Tests for the built-in Codex CLI agent core plugin and host wiring."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from conftest import make_settings

from pynchy.host.container_manager.mounts import build_volume_mounts
from pynchy.plugins import get_plugin_manager
from pynchy.types import WorkspaceProfile


def _group(folder: str = "codex-group") -> WorkspaceProfile:
    return WorkspaceProfile(
        jid=f"{folder}@g.us",
        name="Codex Group",
        folder=folder,
        trigger="@pynchy",
        added_at="2024-01-01",
    )


def test_codex_plugin_info_structure() -> None:
    """The built-in plugin advertises the Codex CLI-backed core."""
    from pynchy.plugins.agent_cores.codex import CodexAgentCorePlugin

    info = CodexAgentCorePlugin().pynchy_agent_core_info()

    assert info == {
        "name": "codex",
        "module": "agent_runner.cores.codex",
        "class_name": "CodexCLIAgentCore",
        "packages": [],
        "host_source_path": None,
    }


def test_codex_plugin_registered_via_static_registry() -> None:
    """Codex is auto-discovered alongside the existing built-in agent cores."""
    with patch("pluggy.PluginManager.load_setuptools_entrypoints", return_value=0):
        pm = get_plugin_manager()

    plugin_names = [pm.get_name(p) for p in pm.get_plugins()]
    core_names = [c["name"] for c in pm.hook.pynchy_agent_core_info()]

    assert "builtin-codex" in plugin_names
    assert "codex" in core_names


def test_agent_dockerfile_installs_codex_as_agent_executable() -> None:
    """The image must not leave codex as an agent-inaccessible /root symlink."""
    dockerfile = Path("src/pynchy/agent/Dockerfile").read_text(encoding="utf-8")
    install_script = Path("src/pynchy/agent/install_codex.sh").read_text(encoding="utf-8")

    assert "COPY install_codex.sh /tmp/install_codex.sh" in dockerfile
    assert 'codex_home="${CODEX_HOME:-/opt/codex}"' in install_script
    assert 'ln -sfn "$standalone_root/current/bin/codex" "$install_dir/codex"' in install_script
    assert 'chmod -R a+rX "$codex_home"' in install_script
    assert "readlink -f /usr/local/bin/codex" not in dockerfile


def test_build_volume_mounts_creates_per_group_codex_home(tmp_path: Path) -> None:
    """Each group gets isolated Codex CLI state mounted at ~/.codex."""
    settings = make_settings(
        project_root=tmp_path,
        groups_dir=tmp_path / "groups",
        data_dir=tmp_path / "data",
    )
    (tmp_path / "groups" / "codex-group").mkdir(parents=True)

    with patch("pynchy.host.container_manager.mounts.get_settings", return_value=settings):
        mounts = build_volume_mounts(_group(), is_admin=False)

    codex_mount = next(m for m in mounts if m.container_path == "/home/agent/.codex")
    codex_home = tmp_path / "data" / "sessions" / "codex-group" / ".codex"
    assert codex_mount.host_path == str(codex_home)
    assert codex_mount.readonly is False
    assert codex_home.is_dir()


def test_build_volume_mounts_does_not_seed_host_codex_auth(tmp_path: Path) -> None:
    """Codex core auth is owned by the gateway, not host ChatGPT state."""
    host_home = tmp_path / "host-home"
    host_auth = host_home / ".codex" / "auth.json"
    host_auth.parent.mkdir(parents=True)
    host_auth.write_text('{"tokens": "host"}')

    settings = make_settings(
        project_root=tmp_path,
        groups_dir=tmp_path / "groups",
        data_dir=tmp_path / "data",
    )
    (tmp_path / "groups" / "codex-group").mkdir(parents=True)

    with (
        patch("pynchy.host.container_manager.mounts.get_settings", return_value=settings),
        patch("pynchy.host.container_manager.mounts.Path.home", return_value=host_home),
    ):
        build_volume_mounts(_group(), is_admin=False)

    group_auth = tmp_path / "data" / "sessions" / "codex-group" / ".codex" / "auth.json"
    assert not group_auth.exists()
