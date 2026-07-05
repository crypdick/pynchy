"""Tests for the claude-cli agent core's stream-json -> AgentEvent mapping.

The core parses raw ``claude`` stdout line-by-line (cores/claude_cli.py). These
tests pin the ``_map_line`` contract -- in particular the hardening that keeps a
"user" message (which on this stream carries only tool_result blocks) from ever
surfacing a text block as an agent ``text`` event.
"""

from __future__ import annotations

import asyncio
import signal

from agent_runner.core import AgentCoreConfig
from agent_runner.cores.claude_cli import ClaudeCLIAgentCore


def _core(session_id: str | None = None) -> ClaudeCLIAgentCore:
    return ClaudeCLIAgentCore(
        AgentCoreConfig(
            cwd="/tmp",
            session_id=session_id,
            group_folder="g",
            chat_jid="j",
            is_admin=False,
            is_scheduled_task=False,
        )
    )


def _types(core: ClaudeCLIAgentCore, obj: dict) -> list[str]:
    return [e.type for e in core._map_line(obj)]


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
    events = _core()._map_line(obj)
    assert len(events) == 1
    e = events[0]
    assert e.type == "tool_result"
    assert e.data["tool_result_id"] == "t1"
    assert e.data["tool_result_content"] == "ok"
    assert e.data["tool_result_is_error"] is False


def test_user_tool_result_list_content_json_encoded():
    obj = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": [1, 2]}]},
    }
    (e,) = _core()._map_line(obj)
    assert e.data["tool_result_content"] == "[1, 2]"


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
    events = _core()._map_line(obj)
    assert [e.type for e in events] == ["thinking", "tool_use", "text"]
    assert events[0].data["thinking"] == "hmm"
    assert events[1].data["tool_name"] == "Bash"
    assert events[1].data["tool_input"] == {"command": "ls"}
    assert events[2].data["text"] == "done"


def test_assistant_bare_string_coerced_to_text():
    obj = {"type": "assistant", "message": {"content": "plain reply"}}
    (e,) = _core()._map_line(obj)
    assert e.type == "text"
    assert e.data["text"] == "plain reply"


# ---------------------------------------------------------------------------
# system / result / unknown
# ---------------------------------------------------------------------------


def test_system_init_captures_session_id():
    core = _core()
    events = core._map_line({"type": "system", "subtype": "init", "session_id": "sid-9"})
    assert core.session_id == "sid-9"
    assert len(events) == 1
    assert events[0].type == "system"
    assert events[0].data["system_subtype"] == "init"


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
    (e,) = core._map_line(obj)
    assert e.type == "result"
    assert e.data["result"] == "all done"
    assert e.data["result_metadata"]["subtype"] == "success"
    assert e.data["result_metadata"]["session_id"] == "sid-r"
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

    def send_signal(self, sig):
        self.signals.append(sig)

    def kill(self):
        self.killed = True

    async def wait(self):
        return 0


def test_stop_sends_sigint_and_clears_proc():
    core = _core()
    proc = _FakeProc(returncode=None)
    core._proc = proc
    asyncio.run(core.stop())
    assert proc.signals == [signal.SIGINT]
    assert proc.killed is False
    assert core._proc is None


def test_stop_escalates_to_kill_on_timeout(monkeypatch):
    core = _core()
    proc = _FakeProc(returncode=None)
    core._proc = proc

    async def _raise_timeout(awaitable=None, *_args, **_kwargs):
        # Close the proc.wait() coroutine we're bypassing so it isn't reported
        # as "never awaited".
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", _raise_timeout)
    asyncio.run(core.stop())
    assert proc.killed is True
    assert core._proc is None


def test_stop_noop_when_already_exited():
    core = _core()
    proc = _FakeProc(returncode=0)  # already finished
    core._proc = proc
    asyncio.run(core.stop())
    assert proc.signals == []
    assert proc.killed is False


def test_stop_noop_when_no_proc():
    core = _core()
    core._proc = None
    asyncio.run(core.stop())  # must not raise
    assert core._proc is None
