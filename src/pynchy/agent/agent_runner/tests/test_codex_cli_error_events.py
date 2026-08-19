"""Codex CLI retry and terminal error event contract tests."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

from agent_runner.core import AgentCoreConfig
from agent_runner.cores.codex import CodexCLIAgentCore
from agent_runner.events import ResultEvent


def _core() -> CodexCLIAgentCore:
    return CodexCLIAgentCore(
        AgentCoreConfig(
            cwd="/home/agent/src/owner/project",
            session_id=None,
            group_folder="g",
            chat_jid="j",
            is_admin=False,
            is_scheduled_task=False,
            mcp_servers={},
            extra={"model": "gpt-5.2-codex"},
        )
    )


class _FakeStdin:
    def write(self, _content: bytes) -> None:
        return None

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FinishedStream:
    def __init__(self, lines: list[bytes], read_result: bytes = b"") -> None:
        self._lines = iter(lines)
        self._read_result = read_result

    def __aiter__(self) -> _FinishedStream:
        return self

    async def __anext__(self) -> bytes:
        try:
            return next(self._lines)
        except StopIteration:
            raise StopAsyncIteration from None

    async def read(self) -> bytes:
        return self._read_result


class _FakeProc:
    def __init__(
        self,
        events: list[dict[str, object]],
        returncode: int = 0,
        stderr: bytes = b"",
    ) -> None:
        self.returncode = returncode
        self.stdin = _FakeStdin()
        self.stdout = _FinishedStream([_json_line(event) for event in events])
        self.stderr = _FinishedStream([], stderr)

    async def wait(self) -> int:
        return self.returncode


def _json_line(event: dict[str, object]) -> bytes:
    return (json.dumps(event) + "\n").encode()


def _run_query(
    events: list[dict[str, object]], returncode: int = 0, stderr: bytes = b""
) -> list[object]:
    core = _core()
    proc = _FakeProc(events, returncode, stderr)

    async def run() -> list[object]:
        with patch(
            "agent_runner.cores.codex.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            return [event async for event in core.query("hello")]

    return asyncio.run(run())


def test_stream_event_prefers_turn_failure_after_pending_error():
    core = _core()

    retry = core.map_stream_event({"type": "error", "message": "Reconnecting... 1/5"})
    failed = core.map_stream_event(
        {"type": "turn.failed", "error": {"message": "request failed", "code": "timeout"}}
    )

    assert retry == []
    assert len(failed) == 1
    assert isinstance(failed[0], ResultEvent)
    assert failed[0].result == "request failed"


def test_stream_event_ignores_error_after_terminal_turn_failure():
    core = _core()

    failed = core.map_stream_event(
        {"type": "turn.failed", "error": {"message": "request failed", "code": "timeout"}}
    )
    duplicate = core.map_stream_event(
        {"type": "error", "error": {"message": "request failed", "code": "timeout"}}
    )

    assert len(failed) == 1
    assert isinstance(failed[0], ResultEvent)
    assert failed[0].result == "request failed"
    assert duplicate == []


def test_query_synthesizes_latest_error_when_process_exits_without_turn_failure():
    events = _run_query(
        [
            {"type": "error", "message": "Reconnecting... 1/5"},
            {
                "type": "error",
                "error": {"message": "connection lost", "code": "stream_disconnected"},
            },
        ],
        returncode=1,
    )

    assert [event.type for event in events] == ["result"]
    assert isinstance(events[0], ResultEvent)
    assert events[0].result == "connection lost"
    assert events[0].result_metadata.subtype == "stream_disconnected"
    assert events[0].result_metadata.is_error is True


def test_query_marks_clean_exit_without_terminal_turn_as_error():
    events = _run_query(
        [
            {"type": "item.started", "item": {"type": "command_execution", "command": "ls"}},
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "ls", "exit_code": 0},
            },
            {"type": "turn.completed", "usage": {"input_tokens": 1}},
        ]
    )

    assert isinstance(events[-1], ResultEvent)
    assert events[-1].result is None
    assert events[-1].result_metadata.subtype == "missing_terminal_turn"
    assert events[-1].result_metadata.is_error is True


def test_query_preserves_stderr_when_process_fails_before_stream_events():
    message = (
        "Error: thread/resume failed: failed to resolve rollout path "
        "/Users/old/.codex/sessions/rollout.jsonl: file does not exist (code -32600)"
    )

    events = _run_query([], returncode=1, stderr=message.encode())

    assert isinstance(events[-1], ResultEvent)
    assert events[-1].result == message
    assert events[-1].result_metadata.subtype == "error"
    assert events[-1].result_metadata.is_error is True


def test_query_discards_retry_notice_after_successful_turn():
    events = _run_query(
        [
            {"type": "error", "message": "Reconnecting... 1/5"},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "recovered"},
            },
            {"type": "turn.completed", "usage": {"input_tokens": 1}},
        ]
    )

    assert [event.type for event in events] == ["text", "result"]
    assert isinstance(events[1], ResultEvent)
    assert events[1].result == "recovered"
    assert events[1].result_metadata.is_error is False
