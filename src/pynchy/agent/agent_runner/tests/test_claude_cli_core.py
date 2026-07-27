"""Tests for the Claude CLI core's public stream-json -> AgentEvent mapping.

The core parses raw ``claude`` stdout line-by-line (cores/claude_cli.py). These
tests pin the ``map_stream_line`` contract -- in particular the hardening that keeps a
"user" message (which on this stream carries only tool_result blocks) from ever
surfacing a text block as an agent ``text`` event.
"""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from unittest.mock import patch

from agent_runner.core import AgentCoreConfig
from agent_runner.cores.claude_cli import ClaudeCLIAgentCore
from agent_runner.events import (
    ResultEvent,
    SystemEvent,
    TextEvent,
    ThinkingEvent,
    ToolResultEvent,
    ToolUseEvent,
)


def _core(session_id: str | None = None) -> ClaudeCLIAgentCore:
    return ClaudeCLIAgentCore(
        AgentCoreConfig(
            cwd=str(Path.cwd()),
            session_id=session_id,
            group_folder="g",
            chat_jid="j",
            is_admin=False,
            is_scheduled_task=False,
        )
    )


def _types(core: ClaudeCLIAgentCore, obj: dict) -> list[str]:
    return [e.type for e in core.map_stream_line(obj)]


# ---------------------------------------------------------------------------
# user messages: only tool_result blocks are mapped
# ---------------------------------------------------------------------------


def test_user_text_block_dropped():
    """A user-message text block (a would-be echo of the prompt) is dropped."""
    obj = {"type": "user", "message": {"content": [{"type": "text", "text": "my prompt"}]}}
    assert _types(_core(), obj) == []


def test_user_bare_string_dropped():
    """A user message with bare-string content is not coerced into a text event."""
    obj = {"type": "user", "message": {"content": "echoed prompt"}}
    assert _types(_core(), obj) == []


def test_user_tool_result_kept():
    obj = {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok", "is_error": False}
            ]
        },
    }
    events = _core().map_stream_line(obj)
    assert len(events) == 1
    e = events[0]
    assert isinstance(e, ToolResultEvent)
    assert e.tool_result_id == "t1"
    assert e.tool_result_content == "ok"
    assert e.tool_result_is_error is False


def test_user_tool_result_list_content_json_encoded():
    obj = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": [1, 2]}]},
    }
    (e,) = _core().map_stream_line(obj)
    assert isinstance(e, ToolResultEvent)
    assert e.tool_result_content == "[1, 2]"


def test_user_mixed_blocks_only_tool_result_survives():
    obj = {
        "type": "user",
        "message": {
            "content": [
                {"type": "text", "text": "echo"},
                {"type": "tool_result", "tool_use_id": "t3", "content": "r"},
            ]
        },
    }
    assert _types(_core(), obj) == ["tool_result"]


# ---------------------------------------------------------------------------
# assistant messages: thinking / tool_use / text all mapped, in order
# ---------------------------------------------------------------------------


def test_assistant_all_block_types_in_order():
    obj = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                {"type": "text", "text": "done"},
            ]
        },
    }
    events = _core().map_stream_line(obj)
    assert [e.type for e in events] == ["thinking", "tool_use", "text"]
    assert isinstance(events[0], ThinkingEvent)
    assert isinstance(events[1], ToolUseEvent)
    assert isinstance(events[2], TextEvent)
    assert events[0].thinking == "hmm"
    assert events[1].tool_name == "Bash"
    assert events[1].tool_input == {"command": "ls"}
    assert events[2].text == "done"


def test_assistant_bare_string_coerced_to_text():
    obj = {"type": "assistant", "message": {"content": "plain reply"}}
    (e,) = _core().map_stream_line(obj)
    assert isinstance(e, TextEvent)
    assert e.text == "plain reply"


# ---------------------------------------------------------------------------
# system / result / unknown
# ---------------------------------------------------------------------------


def test_system_init_captures_session_id():
    core = _core()
    events = core.map_stream_line({"type": "system", "subtype": "init", "session_id": "sid-9"})
    assert core.session_id == "sid-9"
    assert len(events) == 1
    assert isinstance(events[0], SystemEvent)
    assert events[0].system_subtype == "init"


def test_result_maps_metadata_and_updates_session_id():
    core = _core()
    obj = {
        "type": "result",
        "subtype": "success",
        "session_id": "sid-r",
        "is_error": False,
        "num_turns": 2,
        "total_cost_usd": 0.01,
        "result": "all done",
    }
    (e,) = core.map_stream_line(obj)
    assert isinstance(e, ResultEvent)
    assert e.result == "all done"
    assert e.result_metadata.subtype == "success"
    assert e.result_metadata.session_id == "sid-r"
    assert core.session_id == "sid-r"


def test_unknown_type_yields_nothing():
    # e.g. rate_limit_event / stream_event lines the parser intentionally ignores
    assert _types(_core(), {"type": "rate_limit_event"}) == []
    assert _types(_core(), {"type": "stream_event", "event": {}}) == []


# ---------------------------------------------------------------------------
# stop(): SIGINT-first, escalate to kill
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.signals: list[int] = []
        self.killed = False
        self.stdin = _FakeStdin()
        self.stdout = _BlockingStdout()
        self.stderr = _FakeStderr()

    def send_signal(self, sig):
        self.signals.append(sig)

    def kill(self):
        self.killed = True

    async def wait(self):
        return 0


class _FakeStdin:
    def write(self, _content: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeStderr:
    async def read(self) -> bytes:
        return b""


class _BlockingStdout:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        self.started.set()
        await self.release.wait()
        raise StopAsyncIteration


async def _start_active_query(core: ClaudeCLIAgentCore, proc: _FakeProc):
    with patch(
        "agent_runner.cores.claude_cli.asyncio.create_subprocess_exec",
        return_value=proc,
    ):
        query = core.query("stop test")
        next_event = asyncio.create_task(anext(query))
        await proc.stdout.started.wait()
    return query, next_event


async def _finish_query(proc: _FakeProc, next_event: asyncio.Task) -> None:
    proc.stdout.release.set()
    event = await next_event
    assert event.type == "result"


async def _stop_active_query(core: ClaudeCLIAgentCore, proc: _FakeProc) -> None:
    _query, next_event = await _start_active_query(core, proc)
    await core.stop()
    await _finish_query(proc, next_event)


def test_stop_sends_sigint_to_an_active_public_query():
    core = _core()
    proc = _FakeProc(returncode=None)
    asyncio.run(_stop_active_query(core, proc))

    assert proc.signals == [signal.SIGINT]
    assert proc.killed is False


def test_stop_escalates_to_kill_on_timeout(monkeypatch):
    core = _core()
    proc = _FakeProc(returncode=None)

    def _raise_timeout(awaitable=None, *_args, **_kwargs):
        # Close the proc.wait() coroutine we're bypassing so it isn't reported
        # as "never awaited".
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", _raise_timeout)
    asyncio.run(_stop_active_query(core, proc))

    assert proc.killed is True


def test_stop_leaves_an_already_exited_public_query_alone():
    core = _core()
    proc = _FakeProc(returncode=0)  # already finished
    asyncio.run(_stop_active_query(core, proc))

    assert proc.signals == []
    assert proc.killed is False


def test_stop_noop_when_no_proc():
    core = _core()
    asyncio.run(core.stop())  # must not raise
