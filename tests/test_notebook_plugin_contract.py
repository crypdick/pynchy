"""Notebook MCP plugin registration contract."""

from __future__ import annotations

from pynchy.plugins.integrations.notebook_server import NotebookServerPlugin


def test_notebook_plugin_registers_workspace_scoped_docker_server() -> None:
    registration = NotebookServerPlugin().pynchy_mcp_server_spec()

    assert len(registration) == 1
    spec = registration[0]
    assert spec.name == "notebook"
    assert spec.config.type == "docker"
    assert spec.config.inject_workspace
    assert spec.config.volumes == ["groups/{workspace}:/workspace"]
    assert spec.config.extra_ports == [8888]
