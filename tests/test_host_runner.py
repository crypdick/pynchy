"""Tests for direct host agent execution."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from typing import TYPE_CHECKING, Any

import pytest

from pynchy.host.orchestrator.host_runner import run_host_input, stop_host_process
from pynchy.types import ContainerInput, ContainerOutput

if TYPE_CHECKING:
    from pathlib import Path


class _FakeStdin:
    def __init__(self) -> None:
        self.buffer = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buffer += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeStdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __aiter__(self) -> _FakeStdout:
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeStderr:
    def __init__(self, data: bytes = b"") -> None:
        self._data = data

    async def read(self) -> bytes:
        return self._data


class _FakeProcess:
    def __init__(self, stdout_lines: list[bytes], returncode: int | None = 0) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(stdout_lines)
        self.stderr = _FakeStderr()
        self.returncode = returncode
        self.pid = 123
        self.killed = False

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


@pytest.mark.asyncio
async def test_stop_host_process_signals_the_runner_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host turns must stop both the runner and its Codex child, not just the runner."""
    fake_proc = _FakeProcess([], returncode=None)
    signals: list[tuple[int, signal.Signals]] = []

    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    await stop_host_process(fake_proc)

    assert signals == [(fake_proc.pid, signal.SIGINT)]
    assert fake_proc.killed is False


@pytest.mark.asyncio
async def test_run_host_input_streams_jsonl_outputs_without_file_ipc(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    created: dict[str, Any] = {}
    fake_proc = _FakeProcess(
        [
            b'{"status":"success","type":"text","text":"hello"}\n',
            b'{"status":"success","result":"done","new_session_id":"session-1"}\n',
        ]
    )

    async def fake_create_subprocess_exec(*cmd: str, **kwargs: Any) -> _FakeProcess:
        await asyncio.sleep(0)
        created["cmd"] = cmd
        created["kwargs"] = kwargs
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    outputs: list[ContainerOutput] = []

    async def on_output(output: ContainerOutput) -> None:
        await asyncio.sleep(0)
        outputs.append(output)

    input_data = ContainerInput(
        messages=[{"sender_name": "Ada", "timestamp": "t", "content": "hi"}],
        session_id="session-0",
        group_folder="admin-host",
        chat_jid="slack:C123",
        is_admin=True,
        agent_core_module="agent_runner.cores.codex",
        agent_core_class="CodexCLIAgentCore",
    )
    fake_openai_key = "test-key"  # pragma: allowlist secret

    status = await run_host_input(
        input_data,
        cwd=tmp_path,
        on_output=on_output,
        timeout_seconds=5,
        env={
            "OPENAI_BASE_URL": "http://127.0.0.1:4000",
            "OPENAI_API_KEY": fake_openai_key,
        },
    )

    assert status == "success"
    assert outputs[0].type == "text"
    assert outputs[0].text == "hello"
    assert outputs[1].result == "done"
    assert outputs[1].new_session_id == "session-1"
    assert created["cmd"][:3] == (
        "uv",
        "run",
        "--project",
    )
    assert created["cmd"][3].endswith("src/pynchy/agent/agent_runner")
    assert created["cmd"][4] == "python"
    assert created["kwargs"]["cwd"] == str(tmp_path)
    assert created["kwargs"]["env"]["OPENAI_API_KEY"] == fake_openai_key
    assert created["kwargs"]["start_new_session"] is True
    assert fake_proc.stdin.closed is True

    payload = json.loads(fake_proc.stdin.buffer.decode())
    assert payload["cwd"] == str(tmp_path)
    assert payload["input"]["group_folder"] == "admin-host"
    assert payload["input"]["agent_core_module"] == "agent_runner.cores.codex"


@pytest.mark.asyncio
async def test_run_host_input_reports_a_planned_boundary_interrupt_without_an_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A safe host interruption should drain pending input rather than retry as an error."""
    fake_proc = _FakeProcess([], returncode=-2)

    async def fake_create_subprocess_exec(*_cmd: str, **_kwargs: Any) -> _FakeProcess:
        await asyncio.sleep(0)
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    outputs: list[ContainerOutput] = []
    input_data = ContainerInput(
        messages=[{"sender_name": "Ada", "timestamp": "t", "content": "hi"}],
        session_id="session-0",
        group_folder="admin-host",
        chat_jid="slack:C123",
        is_admin=True,
        agent_core_module="agent_runner.cores.codex",
        agent_core_class="CodexCLIAgentCore",
    )

    status = await run_host_input(
        input_data,
        cwd=tmp_path,
        on_output=outputs.append,
        timeout_seconds=5,
        is_interrupted=lambda: True,
    )

    assert status == "interrupted"
    assert outputs == []
