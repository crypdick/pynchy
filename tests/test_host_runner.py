"""Tests for direct host agent execution."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from host_runner_fakes import (
    BlockingStderr as _BlockingStderr,
)
from host_runner_fakes import (
    BlockingStdout as _BlockingStdout,
)
from host_runner_fakes import (
    FakeProcess as _FakeProcess,
)
from host_runner_fakes import (
    FakeStderr as _FakeStderr,
)
from host_runner_fakes import (
    TimedStdout as _TimedStdout,
)

from pynchy.agent_protocol.api import (
    ContainerInput,
    ContainerOutput,
)
from pynchy.host.orchestrator.host_runner import run_host_input, stop_host_process

if TYPE_CHECKING:
    from pathlib import Path


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
    monkeypatch.setenv("UNRELATED_HOST_SECRET", "must-not-leak")

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
        project_root=tmp_path,
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
    assert "UNRELATED_HOST_SECRET" not in created["kwargs"]["env"]
    assert created["kwargs"]["start_new_session"] is True
    assert fake_proc.stdin.closed is True

    payload = json.loads(fake_proc.stdin.buffer.decode())
    assert payload["cwd"] == str(tmp_path)
    assert payload["input"]["group_folder"] == "admin-host"
    assert payload["input"]["agent_core_module"] == "agent_runner.cores.codex"


@pytest.mark.asyncio
async def test_run_host_input_reports_structured_errors_and_ignores_blank_lines(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_proc = _FakeProcess(
        [b"\n", b'{"status":"error","error":"agent failed"}\n'],
    )

    async def fake_create_subprocess_exec(*_cmd: str, **_kwargs: Any) -> _FakeProcess:
        await asyncio.sleep(0)
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    outputs: list[ContainerOutput] = []

    async def on_output(output: ContainerOutput) -> None:
        await asyncio.sleep(0)
        outputs.append(output)

    status = await run_host_input(
        ContainerInput(
            messages=[], group_folder="admin-host", chat_jid="slack:C123", is_admin=False
        ),
        cwd=tmp_path,
        project_root=tmp_path,
        on_output=on_output,
        timeout_seconds=5,
        on_process_started=lambda _proc: True,
    )

    assert status == "error"
    assert [output.error for output in outputs] == ["agent failed"]


@pytest.mark.asyncio
async def test_run_host_input_stops_process_after_malformed_runner_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_proc = _FakeProcess([b"not-json\n"], returncode=None)
    signals: list[tuple[int, signal.Signals]] = []

    async def fake_create_subprocess_exec(*_cmd: str, **_kwargs: Any) -> _FakeProcess:
        await asyncio.sleep(0)
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    with pytest.raises(json.JSONDecodeError):
        await run_host_input(
            ContainerInput(
                messages=[], group_folder="admin-host", chat_jid="slack:C123", is_admin=False
            ),
            cwd=tmp_path,
            project_root=tmp_path,
            on_output=AsyncMock(),
            timeout_seconds=5,
        )

    assert signals == [(fake_proc.pid, signal.SIGINT)]


@pytest.mark.asyncio
async def test_run_host_input_validates_payload_before_spawning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    with pytest.raises(TypeError, match="JSON serializable"):
        await run_host_input(
            ContainerInput(
                messages=[{"content": object()}],
                group_folder="admin-host",
                chat_jid="slack:C123",
                is_admin=False,
            ),
            cwd=tmp_path,
            project_root=tmp_path,
            on_output=AsyncMock(),
            timeout_seconds=5,
        )

    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_host_input_allows_a_runner_without_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_proc = _FakeProcess([])
    fake_proc.stderr = None

    async def fake_create_subprocess_exec(*_cmd: str, **_kwargs: Any) -> _FakeProcess:
        await asyncio.sleep(0)
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    status = await run_host_input(
        ContainerInput(
            messages=[], group_folder="admin-host", chat_jid="slack:C123", is_admin=False
        ),
        cwd=tmp_path,
        project_root=tmp_path,
        on_output=AsyncMock(),
        timeout_seconds=5,
    )

    assert status == "success"


@pytest.mark.asyncio
async def test_run_host_input_rejects_a_process_before_writing_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_proc = _FakeProcess([], returncode=0)
    seen: list[_FakeProcess] = []

    async def fake_create_subprocess_exec(*_cmd: str, **_kwargs: Any) -> _FakeProcess:
        await asyncio.sleep(0)
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    status = await run_host_input(
        ContainerInput(
            messages=[], group_folder="admin-host", chat_jid="slack:C123", is_admin=False
        ),
        cwd=tmp_path,
        project_root=tmp_path,
        on_output=AsyncMock(),
        timeout_seconds=5,
        on_process_started=lambda proc: seen.append(proc) or False,
    )

    assert status == "interrupted"
    assert seen == [fake_proc]
    assert fake_proc.stdin.closed is False


@pytest.mark.asyncio
async def test_run_host_input_stops_process_when_start_callback_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_proc = _FakeProcess([], returncode=None)
    signals: list[tuple[int, signal.Signals]] = []

    async def fake_create_subprocess_exec(*_cmd: str, **_kwargs: Any) -> _FakeProcess:
        await asyncio.sleep(0)
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    def fail_start(_proc: _FakeProcess) -> bool:
        raise RuntimeError("registration failed")

    with pytest.raises(RuntimeError, match="registration failed"):
        await run_host_input(
            ContainerInput(
                messages=[], group_folder="admin-host", chat_jid="slack:C123", is_admin=False
            ),
            cwd=tmp_path,
            project_root=tmp_path,
            on_output=AsyncMock(),
            timeout_seconds=5,
            on_process_started=fail_start,
        )

    assert signals == [(fake_proc.pid, signal.SIGINT)]


@pytest.mark.asyncio
async def test_run_host_input_stops_process_when_stdin_write_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_proc = _FakeProcess([], returncode=None)
    fake_proc.stdin.drain = AsyncMock(side_effect=BrokenPipeError("runner exited"))
    signals: list[tuple[int, signal.Signals]] = []

    async def fake_create_subprocess_exec(*_cmd: str, **_kwargs: Any) -> _FakeProcess:
        await asyncio.sleep(0)
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    with pytest.raises(BrokenPipeError, match="runner exited"):
        await run_host_input(
            ContainerInput(
                messages=[], group_folder="admin-host", chat_jid="slack:C123", is_admin=False
            ),
            cwd=tmp_path,
            project_root=tmp_path,
            on_output=AsyncMock(),
            timeout_seconds=5,
        )

    assert signals == [(fake_proc.pid, signal.SIGINT)]


@pytest.mark.asyncio
async def test_run_host_input_rejects_missing_stdin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_proc = _FakeProcess([], returncode=0)
    fake_proc.stdin = None

    async def fake_create_subprocess_exec(*_cmd: str, **_kwargs: Any) -> _FakeProcess:
        await asyncio.sleep(0)
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(RuntimeError, match="missing stdin"):
        await run_host_input(
            ContainerInput(
                messages=[], group_folder="admin-host", chat_jid="slack:C123", is_admin=False
            ),
            cwd=tmp_path,
            project_root=tmp_path,
            on_output=AsyncMock(),
            timeout_seconds=5,
        )


@pytest.mark.asyncio
async def test_run_host_input_rejects_missing_stdout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_proc = _FakeProcess([], returncode=0)
    fake_proc.stdout = None

    async def fake_create_subprocess_exec(*_cmd: str, **_kwargs: Any) -> _FakeProcess:
        await asyncio.sleep(0)
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(RuntimeError, match="missing stdout"):
        await run_host_input(
            ContainerInput(
                messages=[], group_folder="admin-host", chat_jid="slack:C123", is_admin=False
            ),
            cwd=tmp_path,
            project_root=tmp_path,
            on_output=AsyncMock(),
            timeout_seconds=5,
        )


@pytest.mark.asyncio
async def test_run_host_input_reports_runner_exit_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_proc = _FakeProcess([], returncode=7)
    fake_proc.stderr = _FakeStderr(b"runner crashed")

    async def fake_create_subprocess_exec(*_cmd: str, **_kwargs: Any) -> _FakeProcess:
        await asyncio.sleep(0)
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    outputs: list[ContainerOutput] = []

    async def on_output(output: ContainerOutput) -> None:
        await asyncio.sleep(0)
        outputs.append(output)

    status = await run_host_input(
        ContainerInput(
            messages=[], group_folder="admin-host", chat_jid="slack:C123", is_admin=False
        ),
        cwd=tmp_path,
        project_root=tmp_path,
        on_output=on_output,
        timeout_seconds=5,
    )

    assert status == "error"
    assert outputs[0].error == "Host agent runner exited with code 7: runner crashed"


@pytest.mark.asyncio
async def test_run_host_input_reports_runner_that_does_not_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_proc = _FakeProcess([], returncode=None)
    fake_proc.wait = AsyncMock(side_effect=[TimeoutError, 0])  # type: ignore[method-assign]
    signals: list[tuple[int, signal.Signals]] = []

    async def fake_create_subprocess_exec(*_cmd: str, **_kwargs: Any) -> _FakeProcess:
        await asyncio.sleep(0)
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    outputs: list[ContainerOutput] = []

    async def on_output(output: ContainerOutput) -> None:
        await asyncio.sleep(0)
        outputs.append(output)

    status = await run_host_input(
        ContainerInput(
            messages=[], group_folder="admin-host", chat_jid="slack:C123", is_admin=False
        ),
        cwd=tmp_path,
        project_root=tmp_path,
        on_output=on_output,
        timeout_seconds=5,
    )

    assert status == "error"
    assert outputs[0].error == "Host agent runner did not exit"
    assert signals == [(fake_proc.pid, signal.SIGINT)]


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
        project_root=tmp_path,
        on_output=outputs.append,
        timeout_seconds=5,
        is_interrupted=lambda: True,
    )

    assert status == "interrupted"
    assert outputs == []


@pytest.mark.asyncio
async def test_host_progress_refreshes_timeout_before_slow_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A tool event counts before a slow channel callback consumes it."""
    fake_proc = _FakeProcess([])
    fake_proc.stdout = _TimedStdout(
        [
            (
                0.02,
                (
                    b'{"status":"success","type":"tool_use","tool_name":"exec_command",'
                    b'"tool_input":{"command":"git commit"},"query_id":"query-hooks"}\n'
                ),
            )
        ]
    )

    async def fake_create_subprocess_exec(*_cmd: str, **_kwargs: Any) -> _FakeProcess:
        await asyncio.sleep(0)
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    outputs: list[ContainerOutput] = []

    async def slow_delivery(output: ContainerOutput) -> None:
        await asyncio.sleep(0.02)
        outputs.append(output)

    input_data = ContainerInput(
        messages=[],
        group_folder="admin-host",
        chat_jid="slack:C123",
        is_admin=True,
        turn_id="turn-hooks",
        query_id="query-hooks",
    )
    status = await run_host_input(
        input_data,
        cwd=tmp_path,
        project_root=tmp_path,
        on_output=slow_delivery,
        timeout_seconds=0.03,
    )

    assert status == "success"
    assert [output.type for output in outputs] == ["tool_use"]


@pytest.mark.asyncio
async def test_host_silence_timeout_stops_process_group_and_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A silent direct worker is stopped without leaking its stderr reader."""
    fake_proc = _FakeProcess([], returncode=None)
    stdout = _BlockingStdout()
    stderr = _BlockingStderr()
    fake_proc.stdout = stdout
    fake_proc.stderr = stderr
    signals: list[tuple[int, signal.Signals]] = []

    async def fake_create_subprocess_exec(*_cmd: str, **_kwargs: Any) -> _FakeProcess:
        await asyncio.sleep(0)
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    outputs: list[ContainerOutput] = []

    async def on_output(output: ContainerOutput) -> None:
        await asyncio.sleep(0)
        outputs.append(output)

    status = await run_host_input(
        ContainerInput(
            messages=[],
            group_folder="admin-host",
            chat_jid="slack:C123",
            is_admin=True,
            turn_id="turn-silent",
            query_id="query-silent",
        ),
        cwd=tmp_path,
        project_root=tmp_path,
        on_output=on_output,
        timeout_seconds=0.02,
    )

    assert stdout.started.is_set()
    assert status == "error"
    assert signals == [(fake_proc.pid, signal.SIGINT)]
    assert stderr.cancelled.is_set()
    assert outputs[-1].error == "Host agent runner inactivity timeout"


@pytest.mark.asyncio
async def test_host_silence_timeout_reports_stderr_from_stopped_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_proc = _FakeProcess([], returncode=None)
    fake_proc.stdout = _BlockingStdout()
    fake_proc.stderr = _FakeStderr(b"codex failed before startup")

    async def fake_create_subprocess_exec(*_cmd: str, **_kwargs: Any) -> _FakeProcess:
        await asyncio.sleep(0)
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(os, "killpg", lambda _pid, _sig: None)
    outputs: list[ContainerOutput] = []

    async def on_output(output: ContainerOutput) -> None:
        await asyncio.sleep(0)
        outputs.append(output)

    status = await run_host_input(
        ContainerInput(
            messages=[],
            group_folder="admin-host",
            chat_jid="slack:C123",
            is_admin=True,
            query_id="query-stderr",
        ),
        cwd=tmp_path,
        project_root=tmp_path,
        on_output=on_output,
        timeout_seconds=0.02,
    )

    assert status == "error"
    assert outputs[-1].error == (
        "Host agent runner inactivity timeout: codex failed before startup"
    )


@pytest.mark.asyncio
async def test_host_silent_inflight_tool_does_not_self_refresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A started tool without new output remains subject to the silence deadline."""
    tool_start = (
        b'{"status":"success","type":"tool_use","tool_name":"exec_command",'
        b'"tool_input":{"command":"git commit"},"query_id":"query-wedged"}\n'
    )
    fake_proc = _FakeProcess([], returncode=None)
    fake_proc.stdout = _BlockingStdout(tool_start)
    fake_proc.stderr = _BlockingStderr()
    signals: list[tuple[int, signal.Signals]] = []

    async def fake_create_subprocess_exec(*_cmd: str, **_kwargs: Any) -> _FakeProcess:
        await asyncio.sleep(0)
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    outputs: list[ContainerOutput] = []

    async def on_output(output: ContainerOutput) -> None:
        await asyncio.sleep(0)
        outputs.append(output)

    status = await run_host_input(
        ContainerInput(
            messages=[],
            group_folder="admin-host",
            chat_jid="slack:C123",
            is_admin=True,
            query_id="query-wedged",
        ),
        cwd=tmp_path,
        project_root=tmp_path,
        on_output=on_output,
        timeout_seconds=0.02,
    )

    assert status == "error"
    assert [output.type for output in outputs] == ["tool_use", "result"]
    assert outputs[-1].error == "Host agent runner inactivity timeout"
    assert signals == [(fake_proc.pid, signal.SIGINT)]


@pytest.mark.asyncio
async def test_host_cancellation_stops_process_group_and_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Temporal cancellation should clean local processes and remain cancellative."""
    fake_proc = _FakeProcess([], returncode=None)
    stdout = _BlockingStdout()
    stderr = _BlockingStderr()
    fake_proc.stdout = stdout
    fake_proc.stderr = stderr
    signals: list[tuple[int, signal.Signals]] = []

    async def fake_create_subprocess_exec(*_cmd: str, **_kwargs: Any) -> _FakeProcess:
        await asyncio.sleep(0)
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    turn = asyncio.create_task(
        run_host_input(
            ContainerInput(
                messages=[],
                group_folder="admin-host",
                chat_jid="slack:C123",
                is_admin=True,
                turn_id="turn-cancelled",
                query_id="query-cancelled",
            ),
            cwd=tmp_path,
            project_root=tmp_path,
            on_output=AsyncMock(),
            timeout_seconds=1,
        )
    )
    await stdout.started.wait()

    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    assert signals == [(fake_proc.pid, signal.SIGINT)]
    assert stderr.cancelled.is_set()
