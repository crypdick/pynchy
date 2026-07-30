"""Notebook MCP plugin registration contract."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from pynchy.plugins.integrations.notebook_server import NotebookServerPlugin

if TYPE_CHECKING:
    import pytest


def test_notebook_plugin_registers_workspace_scoped_docker_server() -> None:
    registration = NotebookServerPlugin().pynchy_mcp_server_spec()

    assert len(registration) == 1
    spec = registration[0]
    assert spec.name == "notebook"
    assert spec.config.type == "docker"
    assert spec.config.inject_workspace
    assert spec.config.volumes == ["groups/{workspace}:/workspace"]
    assert spec.config.extra_ports == [8888]


def test_notebook_plugin_imports_without_plugin_host_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "pluggy", None)

    module = runpy.run_path(
        Path(__file__).parents[1] / "src/pynchy/plugins/integrations/notebook_server/_plugin.py"
    )

    assert module["pluggy"] is None
