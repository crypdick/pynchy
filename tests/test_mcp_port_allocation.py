"""Tests for per-instance port allocation and arg placeholder expansion."""

from __future__ import annotations

from unittest.mock import MagicMock

from conftest import make_settings

from pynchy.config.mcp import McpServerConfig
from pynchy.host.container_manager.mcp.lifecycle import (
    _build_placeholders,
    expand_arg_placeholders,
)
from pynchy.host.container_manager.mcp.resolution import McpInstance, resolve_all_instances
from pynchy.host.container_manager.mcp.manager import McpManager

# ---------------------------------------------------------------------------
# expand_arg_placeholders
# ---------------------------------------------------------------------------


class TestExpandArgPlaceholders:
    def test_basic_substitution(self):
        args = ["--port", "{port}", "--host", "0.0.0.0"]
        result = expand_arg_placeholders(args, {"port": "9100"})
        assert result == ["--port", "9100", "--host", "0.0.0.0"]

    def test_multiple_placeholders(self):
        args = ["--dir", "data/{workspace}/profiles", "--port", "{port}"]
        result = expand_arg_placeholders(args, {"workspace": "research", "port": "9101"})
        assert result == ["--dir", "data/research/profiles", "--port", "9101"]

    def test_no_op_passthrough(self):
        args = ["--headless", "--host", "0.0.0.0"]
        result = expand_arg_placeholders(args, {"port": "9100"})
        assert result == ["--headless", "--host", "0.0.0.0"]

    def test_missing_key_left_as_is(self):
        args = ["--dir", "{unknown}"]
        result = expand_arg_placeholders(args, {"port": "9100"})
        assert result == ["--dir", "{unknown}"]

    def test_empty_args(self):
        assert expand_arg_placeholders([], {"port": "9100"}) == []

    def test_empty_placeholders(self):
        args = ["--port", "{port}"]
        assert expand_arg_placeholders(args, {}) == ["--port", "{port}"]


# ---------------------------------------------------------------------------
# _build_placeholders
# ---------------------------------------------------------------------------


class TestBuildPlaceholders:
    def _make_instance(self, *, port=None, kwargs=None):
        cfg = McpServerConfig(type="script", command="npx", port=port or 9100)
        return McpInstance(
            server_name="browser",
            server_config=cfg,
            kwargs=kwargs or {},
            instance_id="browser",
            container_name="pynchy-mcp-browser",
            port=port,
        )

    def test_includes_port(self):
        inst = self._make_instance(port=9101)
        placeholders = _build_placeholders(inst)
        assert placeholders["port"] == "9101"

    def test_includes_kwargs(self):
        inst = self._make_instance(port=9100, kwargs={"workspace": "sandbox1"})
        placeholders = _build_placeholders(inst)
        assert placeholders["workspace"] == "sandbox1"
        assert placeholders["port"] == "9100"

    def test_no_port_when_none(self):
        inst = self._make_instance(port=None)
        placeholders = _build_placeholders(inst)
        assert "port" not in placeholders


# ---------------------------------------------------------------------------
# _resolve_all_instances port offset
# ---------------------------------------------------------------------------


class TestResolveAllInstancesPortOffset:
    """Port allocation in _resolve_all_instances.

    inject_workspace=True creates separate instances per workspace (each
    gets workspace=<folder> in kwargs → unique instance ID).  Without it,
    two workspaces sharing the same server share one instance.
    """

    def _make_manager(self, workspaces: dict, mcp_servers: dict):
        from pynchy.config.models import WorkspaceConfig

        ws_configs = {}
        for name, servers in workspaces.items():
            ws_configs[name] = WorkspaceConfig(mcp_servers=servers)

        settings = make_settings(
            workspaces=ws_configs,
            mcp_servers={name: McpServerConfig(**spec) for name, spec in mcp_servers.items()},
        )
        gateway = MagicMock()
        return McpManager(settings, gateway)

    def test_inject_workspace_two_workspaces_get_different_ports(self):
        mgr = self._make_manager(
            workspaces={
                "ws1": ["browser"],
                "ws2": ["browser"],
            },
            mcp_servers={
                "browser": {
                    "type": "script",
                    "command": "npx",
                    "port": 9100,
                    "inject_workspace": True,
                },
            },
        )
        state = resolve_all_instances(mgr._settings, mgr._merged_mcp_servers)
        ports = sorted(inst.port for inst in state.instances.values())
        assert ports == [9100, 9101]

    def test_single_workspace_gets_base_port(self):
        mgr = self._make_manager(
            workspaces={"ws1": ["browser"]},
            mcp_servers={
                "browser": {
                    "type": "script",
                    "command": "npx",
                    "port": 9100,
                },
            },
        )
        state = resolve_all_instances(mgr._settings, mgr._merged_mcp_servers)
        inst = list(state.instances.values())[0]
        assert inst.port == 9100

    def test_inject_workspace_independent_port_counters_per_server(self):
        mgr = self._make_manager(
            workspaces={
                "ws1": ["browser", "notebook"],
                "ws2": ["browser", "notebook"],
            },
            mcp_servers={
                "browser": {
                    "type": "script",
                    "command": "npx",
                    "port": 9100,
                    "inject_workspace": True,
                },
                "notebook": {
                    "type": "script",
                    "command": "uv",
                    "port": 8888,
                    "inject_workspace": True,
                },
            },
        )
        state = resolve_all_instances(mgr._settings, mgr._merged_mcp_servers)
        browser_ports = sorted(
            inst.port for inst in state.instances.values() if inst.server_name == "browser"
        )
        notebook_ports = sorted(
            inst.port for inst in state.instances.values() if inst.server_name == "notebook"
        )
        assert browser_ports == [9100, 9101]
        assert notebook_ports == [8888, 8889]

    def test_shared_instance_no_duplicate_port(self):
        """Two workspaces with no per-workspace kwargs share one instance."""
        mgr = self._make_manager(
            workspaces={
                "ws1": ["search"],
                "ws2": ["search"],
            },
            mcp_servers={
                "search": {
                    "type": "script",
                    "command": "node",
                    "port": 7000,
                },
            },
        )
        state = resolve_all_instances(mgr._settings, mgr._merged_mcp_servers)
        # Same instance ID (no kwargs → no hash), so only one instance
        assert len(state.instances) == 1
        inst = list(state.instances.values())[0]
        assert inst.port == 7000

    def test_url_type_gets_none_port(self):
        mgr = self._make_manager(
            workspaces={"ws1": ["remote"]},
            mcp_servers={
                "remote": {
                    "type": "url",
                    "url": "https://example.com/mcp",
                },
            },
        )
        state = resolve_all_instances(mgr._settings, mgr._merged_mcp_servers)
        inst = list(state.instances.values())[0]
        assert inst.port is None
