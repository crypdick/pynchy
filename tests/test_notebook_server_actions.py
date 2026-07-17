"""Hermetic lifecycle coverage for the notebook MCP server actions."""

from __future__ import annotations

import importlib
import sys
import types
from typing import Any
from unittest.mock import AsyncMock

import pytest

from pynchy.plugins.integrations.notebook_server import KernelSession


class _FakeMcp:
    def __init__(self, _name: str) -> None:
        return None

    def tool(self):
        def decorate(function):
            return function

        return decorate


class _FakeKernelClient:
    def __init__(self) -> None:
        self.channels_started = False
        self.channels_stopped = False

    def start_channels(self) -> None:
        self.channels_started = True

    def stop_channels(self) -> None:
        self.channels_stopped = True

    def wait_for_ready(self, *, timeout: int) -> None:
        assert timeout == 30

    def execute(self, _code: str) -> str:
        return "fake-message-id"

    def get_iopub_msg(self, timeout: int) -> dict[str, object]:
        assert timeout == 300
        return {}


class _FakeKernelManager:
    instances: list[_FakeKernelManager] = []

    def __init__(self, *, kernel_name: str) -> None:
        assert kernel_name == "python3"
        self.client_instance = _FakeKernelClient()
        self.started_in: str | None = None
        self.shutdown = False
        type(self).instances.append(self)

    def start_kernel(self, *, cwd: str) -> None:
        self.started_in = cwd

    def client(self) -> _FakeKernelClient:
        return self.client_instance

    def shutdown_kernel(self, *, now: bool) -> None:
        assert now is True
        self.shutdown = True


@pytest.fixture
def notebook_server(monkeypatch: pytest.MonkeyPatch, tmp_path) -> dict[str, Any]:
    """Load the sidecar entry point with its optional process dependencies faked."""
    _FakeKernelManager.instances.clear()
    fastmcp = types.ModuleType("fastmcp")
    fastmcp.FastMCP = _FakeMcp
    jupyter_client = types.ModuleType("jupyter_client")
    jupyter_client.KernelManager = _FakeKernelManager
    monkeypatch.setitem(sys.modules, "fastmcp", fastmcp)
    monkeypatch.setitem(sys.modules, "jupyter_client", jupyter_client)
    monkeypatch.setattr(sys, "argv", ["notebook-server", "--workspace-dir", str(tmp_path)])
    monkeypatch.delitem(
        sys.modules,
        "pynchy.plugins.integrations.notebook_server.__main__",
        raising=False,
    )

    server = vars(importlib.import_module("pynchy.plugins.integrations.notebook_server.__main__"))
    yield server
    server["_sessions"].clear()


@pytest.mark.action("notebook.kernel.start")
@pytest.mark.asyncio
async def test_start_kernel_creates_a_persisted_kernel_session(
    notebook_server: dict[str, Any],
) -> None:
    notebook_server["execute_code"] = AsyncMock(return_value=[])

    started = await notebook_server["start_kernel"]("coverage")

    assert started["status"] == "started"
    assert started["notebook"] == "coverage.qmd"
    assert _FakeKernelManager.instances[0].started_in == str(notebook_server["WORKSPACE_DIR"])


@pytest.mark.action("notebook.file.save")
@pytest.mark.asyncio
async def test_save_as_persists_stream_output_in_its_notebook(
    notebook_server: dict[str, Any],
) -> None:
    kernel_id = "kernel-output"
    kernel_manager = _FakeKernelManager(kernel_name="python3")
    notebook_server["_sessions"][kernel_id] = KernelSession(
        kernel_id,
        kernel_manager,
        kernel_manager.client(),
        "coverage",
    )
    notebook_server["execute_code"] = AsyncMock(
        return_value=[{"output_type": "stream", "name": "stdout", "text": "coverage"}]
    )

    await notebook_server["execute_cell"](kernel_id, "print('coverage')")
    result = await notebook_server["save_as"](kernel_id, "coverage.ipynb")

    assert result == {"notebook": "coverage.ipynb", "cells": 1}


@pytest.mark.action("notebook.cell.execute")
@pytest.mark.action("notebook.markdown.add")
@pytest.mark.action("notebook.file.save")
@pytest.mark.action("notebook.file.read")
@pytest.mark.action("notebook.file.list")
@pytest.mark.action("notebook.kernel.list")
@pytest.mark.action("notebook.kernel.stop")
@pytest.mark.asyncio
async def test_notebook_actions_persist_and_clean_up_one_kernel(
    notebook_server: dict[str, Any],
) -> None:
    """Exercise each public notebook state transition through the MCP action functions."""

    def fake_execute(_session: object, code: str) -> list[dict[str, object]]:
        assert code == "print('coverage')"
        return []

    notebook_server["execute_code"] = AsyncMock(side_effect=fake_execute)

    kernel_id = "kernel-coverage"
    kernel_manager = _FakeKernelManager(kernel_name="python3")
    kernel_client = kernel_manager.client()
    notebook_server["_sessions"][kernel_id] = KernelSession(
        kernel_id,
        kernel_manager,
        kernel_client,
        "coverage",
    )

    executed = await notebook_server["execute_cell"](kernel_id, "print('coverage')")
    assert executed == {
        "cell_number": 1,
        "outputs": [],
    }

    markdown = await notebook_server["add_markdown"](kernel_id, "# Coverage")
    assert markdown == {"cell_number": 2, "notebook": "coverage.qmd"}

    saved = await notebook_server["save_as"](kernel_id, "coverage.ipynb")
    assert saved == {"notebook": "coverage.ipynb", "cells": 2}

    read = await notebook_server["read_notebook"]("coverage.ipynb")
    assert read["notebook"] == "coverage.ipynb"
    assert [cell["type"] for cell in read["cells"]] == ["code", "markdown"]

    listed = await notebook_server["list_notebooks"]()
    assert listed["count"] == 2
    assert {notebook["name"] for notebook in listed["notebooks"]} == {
        "coverage.ipynb",
        "coverage.qmd",
    }

    kernels = await notebook_server["list_kernels"]()
    assert kernels == {
        "kernels": [{"kernel_id": kernel_id, "notebook": "coverage.qmd", "cells": 2}],
        "count": 1,
    }

    stopped = await notebook_server["shutdown_kernel"](kernel_id)
    assert stopped == {"notebook": "coverage.qmd", "cells": 2, "status": "shutdown"}
    assert kernel_client.channels_stopped is True
    assert kernel_manager.shutdown is True
