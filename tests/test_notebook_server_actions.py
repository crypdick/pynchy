"""Hermetic lifecycle coverage for the notebook MCP server actions."""

from __future__ import annotations

import importlib
import inspect
import sys
import types
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook

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
        self.channels_stopped = False

    def start_channels(self) -> None:
        pass

    def stop_channels(self) -> None:
        self.channels_stopped = True

    def wait_for_ready(self, *, timeout: int) -> None:
        assert timeout == 30

    def execute(self, _code: str) -> str:
        return "fake-message-id"

    def get_iopub_msg(self, timeout: int) -> dict[str, object]:
        assert timeout == 300
        return {}


class _ScriptedKernelClient(_FakeKernelClient):
    def __init__(self, messages: list[dict[str, object]]) -> None:
        super().__init__()
        self._messages = messages
        self.executed: list[str] = []

    def execute(self, code: str) -> str:
        self.executed.append(code)
        return "message-id"

    def get_iopub_msg(self, timeout: int) -> dict[str, object]:
        assert timeout == 300
        return self._messages.pop(0)


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


class _FailingKernelClient(_FakeKernelClient):
    def wait_for_ready(self, *, timeout: int) -> None:
        assert timeout == 30
        raise RuntimeError("kernel unavailable")


class _FailingKernelManager(_FakeKernelManager):
    def __init__(self, *, kernel_name: str) -> None:
        super().__init__(kernel_name=kernel_name)
        self.client_instance = _FailingKernelClient()


@pytest.fixture
def notebook_server(monkeypatch: pytest.MonkeyPatch, tmp_path) -> dict[str, Any]:
    """Load the sidecar entry point with its optional process dependencies faked."""
    _FakeKernelManager.instances.clear()
    fastmcp = types.ModuleType("fastmcp")
    fastmcp.FastMCP = _FakeMcp
    jupyter_client = types.ModuleType("jupyter_client")
    jupyter_client.KernelManager = _FakeKernelManager
    ubuntu_namer = types.ModuleType("ubuntu_namer")
    ubuntu_namer.generate_name = lambda **_kwargs: "calm-capybara"
    monkeypatch.setitem(sys.modules, "fastmcp", fastmcp)
    monkeypatch.setitem(sys.modules, "jupyter_client", jupyter_client)
    monkeypatch.setitem(sys.modules, "ubuntu_namer", ubuntu_namer)
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


@pytest.mark.asyncio
async def test_start_kernel_generates_a_date_prefixed_notebook_name(
    monkeypatch: pytest.MonkeyPatch,
    notebook_server: dict[str, Any],
) -> None:
    monkeypatch.setattr(
        "ubuntu_namer.generate_name",
        lambda **_kwargs: "calm-capybara",
        raising=False,
    )
    notebook_server["execute_code"] = AsyncMock(return_value=[])

    started = await notebook_server["start_kernel"]()

    assert started["status"] == "started"
    assert started["notebook"].startswith(f"{datetime.now(UTC).date().isoformat()}-")
    assert started["notebook"].endswith(".qmd")


@pytest.mark.asyncio
async def test_start_kernel_reports_readiness_failure_and_shuts_down_kernel(
    notebook_server: dict[str, Any],
) -> None:
    notebook_server["KernelManager"] = _FailingKernelManager

    result = await notebook_server["start_kernel"]("unavailable")

    assert result == {"error": "Kernel failed to start: kernel unavailable"}
    assert _FailingKernelManager.instances[0].shutdown is True


@pytest.mark.asyncio
async def test_start_kernel_rehydrates_code_and_reports_replay_errors(
    notebook_server: dict[str, Any],
) -> None:
    notebook = new_notebook(
        cells=[
            new_markdown_cell(source="Context"),
            new_code_cell(source="1 / 0"),
        ]
    )
    notebook_server["save_notebook"](
        notebook,
        notebook_server["NOTEBOOK_DIR"] / "restore.qmd",
    )
    notebook_server["execute_code"] = AsyncMock(
        side_effect=[
            [],
            [
                {
                    "output_type": "error",
                    "ename": "ZeroDivisionError",
                    "evalue": "division by zero",
                    "traceback": [],
                }
            ],
        ]
    )

    result = await notebook_server["start_kernel"]("restore.qmd")

    assert result["status"] == "rehydrated"
    assert result["cells_total"] == 2
    assert result["code_cells_executed"] == 1
    assert result["replay_errors"] == ["Cell 1: ZeroDivisionError: division by zero"]


@pytest.mark.asyncio
async def test_start_kernel_rehydrates_without_replay_errors(
    notebook_server: dict[str, Any],
) -> None:
    notebook_server["save_notebook"](
        new_notebook(cells=[new_code_cell(source="2 + 2")]),
        notebook_server["NOTEBOOK_DIR"] / "clean-restore.qmd",
    )
    notebook_server["execute_code"] = AsyncMock(return_value=[])

    result = await notebook_server["start_kernel"]("clean-restore.qmd")

    assert result["status"] == "rehydrated"
    assert "replay_errors" not in result


def test_default_workspace_uses_project_root_environment(
    monkeypatch: pytest.MonkeyPatch,
    notebook_server: dict[str, Any],
    tmp_path,
) -> None:
    monkeypatch.setenv("PYNCHY_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["notebook-server", "--workspace", "research"])
    monkeypatch.delitem(
        sys.modules,
        "pynchy.plugins.integrations.notebook_server.__main__",
        raising=False,
    )

    server = vars(importlib.import_module("pynchy.plugins.integrations.notebook_server.__main__"))

    assert server["WORKSPACE_DIR"] == (tmp_path / "groups" / "research").resolve()
    assert server["NOTEBOOK_DIR"].is_dir()
    server["_sessions"].clear()


def test_start_jupyterlab_launches_fixed_viewer_process(
    notebook_server: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    process = Mock()
    with patch.object(notebook_server["subprocess"], "Popen", return_value=process) as popen:
        notebook_server["_start_jupyterlab"]()

    assert notebook_server["_state"].lab_process is process
    argv = popen.call_args.args[0]
    assert argv[:3] == [sys.executable, "-m", "jupyterlab"]
    assert f"--notebook-dir={notebook_server['NOTEBOOK_DIR']}" in argv
    assert "JupyterLab started on port 8888" in capsys.readouterr().out


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
@pytest.mark.asyncio
async def test_execute_cell_collects_matching_kernel_message_types(
    notebook_server: dict[str, Any],
) -> None:
    kernel_id = "kernel-messages"
    messages = [
        {
            "parent_header": {"msg_id": "other-message"},
            "msg_type": "stream",
            "content": {"name": "stdout", "text": "ignore me"},
        },
        {
            "parent_header": {"msg_id": "message-id"},
            "msg_type": "stream",
            "content": {"name": "stdout", "text": "hello\n"},
        },
        {
            "parent_header": {"msg_id": "message-id"},
            "msg_type": "execute_result",
            "content": {"data": {"text/plain": "2"}, "metadata": {}, "execution_count": 1},
        },
        {
            "parent_header": {"msg_id": "message-id"},
            "msg_type": "display_data",
            "content": {"data": {"text/plain": "shown"}, "metadata": {}},
        },
        {
            "parent_header": {"msg_id": "message-id"},
            "msg_type": "comm",
            "content": {},
        },
        {
            "parent_header": {"msg_id": "message-id"},
            "msg_type": "error",
            "content": {
                "ename": "ValueError",
                "evalue": "bad input",
                "traceback": ["Traceback", "ValueError: bad input"],
            },
        },
        {
            "parent_header": {"msg_id": "message-id"},
            "msg_type": "status",
            "content": {"execution_state": "idle"},
        },
    ]
    kernel_manager = _FakeKernelManager(kernel_name="python3")
    kernel_client = _ScriptedKernelClient(messages)
    kernel_manager.client_instance = kernel_client
    notebook_server["_sessions"][kernel_id] = KernelSession(
        kernel_id,
        kernel_manager,
        kernel_client,
        "coverage",
    )

    result = await notebook_server["execute_cell"](kernel_id, "1 + 1")

    assert kernel_client.executed == ["1 + 1"]
    assert result == {
        "cell_number": 1,
        "outputs": [
            {"type": "stream", "name": "stdout", "text": "hello\n"},
            {"type": "result", "text": "2"},
            {"type": "display", "text": "shown"},
            {
                "type": "error",
                "ename": "ValueError",
                "evalue": "bad input",
                "traceback": "Traceback\nValueError: bad input",
            },
        ],
    }


@pytest.mark.action("notebook.cell.execute")
@pytest.mark.asyncio
async def test_execute_cell_returns_timeout_output_when_kernel_stalls(
    monkeypatch: pytest.MonkeyPatch,
    notebook_server: dict[str, Any],
) -> None:
    kernel_id = "kernel-timeout"
    kernel_manager = _FakeKernelManager(kernel_name="python3")
    notebook_server["_sessions"][kernel_id] = KernelSession(
        kernel_id,
        kernel_manager,
        kernel_manager.client(),
        "coverage",
    )

    async def wait_for_then_timeout(awaitable: Any, **options: int) -> object:
        assert options == {"timeout": 310}
        await awaitable
        raise TimeoutError

    execution_asyncio = inspect.unwrap(notebook_server["execute_code"]).__globals__["asyncio"]
    monkeypatch.setattr(execution_asyncio, "wait_for", wait_for_then_timeout)

    result = await notebook_server["execute_cell"](kernel_id, "slow()")

    assert result == {
        "cell_number": 1,
        "outputs": [
            {
                "type": "error",
                "ename": "Timeout",
                "evalue": "Cell execution timed out (5 min)",
                "traceback": "",
            }
        ],
    }


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


@pytest.mark.asyncio
async def test_notebook_actions_report_unknown_kernel_ids(
    notebook_server: dict[str, Any],
) -> None:
    assert "No active kernel" in (await notebook_server["execute_cell"]("missing", "1"))["error"]
    assert "No active kernel" in (await notebook_server["add_markdown"]("missing", "text"))["error"]
    assert "No active kernel" in (await notebook_server["save_as"]("missing", "copy"))["error"]
    assert (await notebook_server["shutdown_kernel"]("missing")) == {
        "error": "No active kernel with id 'missing'."
    }


@pytest.mark.asyncio
async def test_read_notebook_tries_the_other_format_before_reporting_missing(
    notebook_server: dict[str, Any],
) -> None:
    notebook_server["save_notebook"](
        new_notebook(cells=[new_markdown_cell(source="Context")]),
        notebook_server["NOTEBOOK_DIR"] / "alternate.qmd",
    )

    alternate = await notebook_server["read_notebook"]("alternate.ipynb")
    missing = await notebook_server["read_notebook"]("missing.ipynb")

    assert alternate["notebook"] == "alternate.qmd"
    assert missing["error"].startswith("Notebook 'missing.ipynb' not found")
