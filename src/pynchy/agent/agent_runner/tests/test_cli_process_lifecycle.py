"""CLI cores must finish and release real child processes."""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import sys

import pytest
import pytest_asyncio

from agent_runner.core import AgentCoreConfig
from agent_runner.cores.claude_cli import ClaudeCLIAgentCore
from agent_runner.cores.codex import CodexCLIAgentCore
from agent_runner.events import ResultEvent


@pytest.fixture(params=[ClaudeCLIAgentCore, CodexCLIAgentCore])
def core(request, tmp_path):
    return request.param(
        AgentCoreConfig(
            cwd=str(tmp_path),
            session_id=None,
            group_folder="test",
            chat_jid="test-chat",
            is_admin=False,
            is_scheduled_task=False,
        )
    )


@pytest_asyncio.fixture
async def child_program(monkeypatch, tmp_path):
    program = tmp_path / "cli.py"
    children = []
    real_spawn = asyncio.create_subprocess_exec

    async def spawn(*_args, **kwargs):
        child = await real_spawn(sys.executable, str(program), **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    try:
        yield program, children
    finally:
        for child in children:
            if child.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    child.kill()
            await child.communicate()


def _waiting_child(core, *, ignore_interrupt=False):
    ready = (
        {"type": "system", "subtype": "init", "session_id": "test-session"}
        if isinstance(core, ClaudeCLIAgentCore)
        else {"type": "thread.started", "thread_id": "test-session"}
    )
    return f"""
import signal
import sys
from pathlib import Path

def interrupt(*_args):
    Path('interrupted').write_text('SIGINT')
    if not {ignore_interrupt!r}:
        sys.exit(0)

signal.signal(signal.SIGINT, interrupt)
sys.stdin.buffer.read()
sys.stdout.write({(json.dumps(ready) + chr(10))!r})
sys.stdout.flush()
while True:
    signal.pause()
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("stderr_megabytes", [0, 80])
async def test_query_finishes_when_child_fills_stderr(core, child_program, stderr_megabytes):
    program, children = child_program
    wire_events = (
        [{"type": "result", "subtype": "success", "result": "finished"}]
        if isinstance(core, ClaudeCLIAgentCore)
        else [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "finished"}},
            {"type": "turn.completed"},
        ]
    )
    wire = "\nnot-json\n[]\nnull\n" + "".join(json.dumps(event) + "\n" for event in wire_events)
    program.write_text(f"""
import sys
sys.stdin.buffer.read()
sys.stderr.write('diagnostic\\n')
for _ in range({stderr_megabytes}):
    sys.stderr.buffer.write(b'x' * 1024 * 1024)
sys.stderr.flush()
sys.stdout.write({wire!r})
sys.stdout.flush()
""")
    await core.stop()
    async with asyncio.timeout(5):
        events = [event async for event in core.query("run test")]
    results = [event for event in events if isinstance(event, ResultEvent)]
    assert len(results) == 1
    assert results[0].result == "finished"
    assert results[0].result_metadata.is_error is False
    await core.stop()
    assert children[0].returncode == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("ignore_interrupt", [False, True])
async def test_stop_checkpoints_then_reaps_the_child(
    core, child_program, ignore_interrupt, monkeypatch
):
    program, children = child_program
    program.write_text(_waiting_child(core, ignore_interrupt=ignore_interrupt))
    real_wait_for = asyncio.wait_for

    async def short_wait(awaitable, **kwargs):
        return await real_wait_for(awaitable, min(kwargs["timeout"], 0.5))

    monkeypatch.setattr(asyncio, "wait_for", short_wait)
    query = core.query("stop test")
    try:
        async with asyncio.timeout(3):
            await anext(query)
            await core.stop()
        assert (program.parent / "interrupted").read_text() == "SIGINT"
        assert children[0].returncode == (-signal.SIGKILL if ignore_interrupt else 0)
        await core.stop()
    finally:
        await query.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_closing_or_cancelling_query_reaps_the_child(core, child_program, cancel):
    program, children = child_program
    program.write_text(_waiting_child(core))
    query = core.query("cancel test")
    try:
        async with asyncio.timeout(3):
            await anext(query)
            if cancel:
                pending = asyncio.create_task(anext(query))
                await asyncio.sleep(0)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            else:
                await query.aclose()
        assert children[0].returncode == 0
    finally:
        await query.aclose()


@pytest.mark.asyncio
async def test_failed_stderr_reader_still_reaps_child(core, child_program, monkeypatch):
    program, children = child_program
    program.write_text(_waiting_child(core))
    read = asyncio.StreamReader.read

    async def failing_stderr(stream, *args, **kwargs):
        if children and stream is children[0].stderr:
            raise OSError("stderr read failed")
        return await read(stream, *args, **kwargs)

    query = core.query("read failure test")
    with monkeypatch.context() as patch:
        patch.setattr(asyncio.StreamReader, "read", failing_stderr)
        async with asyncio.timeout(3):
            await anext(query)
            with pytest.raises(OSError, match="stderr read failed"):
                await query.aclose()
    assert children[0].returncode == 0
