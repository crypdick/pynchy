"""Behavioral tests for the public ask_user agent tool."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from agent_runner.agent_tools import AgentToolRuntime, call_tool, use_agent_tool_runtime

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def ipc_dirs(tmp_path: Path) -> dict[str, Path]:
    """Create a complete IPC workspace for one agent-tool invocation."""
    responses = tmp_path / "responses"
    responses.mkdir()
    return {"responses": responses, "requests": tmp_path / "requests", "ipc": tmp_path}


@pytest.fixture(autouse=True)
def agent_tool_runtime(ipc_dirs: dict[str, Path]):
    """Route public tool calls through an isolated, explicit runtime context."""
    with use_agent_tool_runtime(
        AgentToolRuntime(
            chat_jid="test@g.us",
            group_folder="test-group",
            is_admin=False,
            is_scheduled_task=False,
            ipc_dir=ipc_dirs["ipc"],
        )
    ):
        yield


def _write_response(
    responses_dir: Path,
    request_id: str,
    *,
    result: dict | None = None,
    error: str | None = None,
) -> None:
    """Write a response atomically, matching the host IPC contract."""
    data: dict = {}
    if error:
        data["error"] = error
    if result is not None:
        data["result"] = result

    final = responses_dir / f"{request_id}.json"
    temporary = final.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data))
    temporary.rename(final)


async def _respond_to_request(
    ipc_dirs: dict[str, Path],
    *,
    result: dict | None = None,
    error: str | None = None,
    delay_seconds: float = 0.0,
) -> dict:
    """Wait for a real IPC request, respond to it, and return its envelope."""
    for _ in range(50):
        request_files = list(ipc_dirs["requests"].glob("*.json"))
        if request_files:
            request = json.loads(request_files[0].read_text(encoding="utf-8"))
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
            _write_response(
                ipc_dirs["responses"],
                request["request_id"],
                result=result,
                error=error,
            )
            return request
        await asyncio.sleep(0.02)
    raise AssertionError("agent tool never wrote an IPC request")


class TestAskUserIPCRequest:
    """The public tool writes a canonical request and returns the host response."""

    @pytest.mark.asyncio
    async def test_sends_correct_type_and_payload(self, ipc_dirs: dict[str, Path]) -> None:
        responder = asyncio.create_task(_respond_to_request(ipc_dirs, result={"answers": ["yes"]}))

        questions = [{"question": "Continue?"}]
        await asyncio.wait_for(call_tool("ask_user", {"questions": questions}), timeout=10.0)
        request = await responder

        assert request["kind"] == "ask_user:ask"
        assert request["payload"]["questions"] == questions

    @pytest.mark.asyncio
    async def test_returns_answer(self, ipc_dirs: dict[str, Path]) -> None:
        responder = asyncio.create_task(
            _respond_to_request(
                ipc_dirs,
                result={"answers": [{"text": "yes, go ahead"}]},
                delay_seconds=0.1,
            )
        )

        result = await asyncio.wait_for(
            call_tool("ask_user", {"questions": [{"question": "Should I proceed?"}]}),
            timeout=10.0,
        )
        await responder

        assert json.loads(result[0].text) == {"answers": [{"text": "yes, go ahead"}]}

    @pytest.mark.asyncio
    async def test_timeout_returns_error(self, ipc_dirs: dict[str, Path]) -> None:
        runtime = AgentToolRuntime(
            chat_jid="test@g.us",
            group_folder="test-group",
            is_admin=False,
            is_scheduled_task=False,
            ipc_dir=ipc_dirs["ipc"],
            ask_user_timeout_seconds=0.01,
        )
        with use_agent_tool_runtime(runtime):
            result = await call_tool("ask_user", {"questions": [{"question": "Hello?"}]})

        assert "timed out" in result[0].text.lower()

    @pytest.mark.asyncio
    async def test_questions_with_options(self, ipc_dirs: dict[str, Path]) -> None:
        questions = [
            {
                "question": "Which option?",
                "options": [
                    {"label": "Option A", "description": "First choice"},
                    {"label": "Option B", "description": "Second choice"},
                ],
            }
        ]
        responder = asyncio.create_task(
            _respond_to_request(ipc_dirs, result={"answers": ["Option A"]})
        )

        await asyncio.wait_for(call_tool("ask_user", {"questions": questions}), timeout=10.0)
        request = await responder

        assert request["payload"]["questions"] == questions


class TestAskUserHandler:
    """The public handler validates before sending an IPC request."""

    @pytest.mark.asyncio
    async def test_empty_questions_returns_error(self) -> None:
        result = await call_tool("ask_user", {"questions": []})
        assert result.isError is True
        assert "non-empty" in result.content[0].text.lower()

    @pytest.mark.asyncio
    async def test_missing_questions_returns_error(self) -> None:
        result = await call_tool("ask_user", {})
        assert result.isError is True
        assert "non-empty" in result.content[0].text.lower()

    @pytest.mark.asyncio
    async def test_handler_forwards_questions(self, ipc_dirs: dict[str, Path]) -> None:
        responder = asyncio.create_task(_respond_to_request(ipc_dirs, result={"answers": ["42"]}))

        result = await asyncio.wait_for(
            call_tool("ask_user", {"questions": [{"question": "What is the answer?"}]}),
            timeout=10.0,
        )
        await responder

        assert "42" in result[0].text


class TestAskUserResponses:
    """Public ask_user responses propagate errors and clean up consumed files."""

    @pytest.mark.asyncio
    async def test_error_response_is_propagated(self, ipc_dirs: dict[str, Path]) -> None:
        responder = asyncio.create_task(
            _respond_to_request(ipc_dirs, error="channel unavailable", delay_seconds=0.05)
        )

        result = await asyncio.wait_for(
            call_tool("ask_user", {"questions": [{"question": "Hello?"}]}),
            timeout=10.0,
        )
        await responder

        assert result[0].text == "Error: channel unavailable"

    @pytest.mark.asyncio
    async def test_response_file_is_deleted_after_read(self, ipc_dirs: dict[str, Path]) -> None:
        responder = asyncio.create_task(
            _respond_to_request(ipc_dirs, result={"answers": ["done"]}, delay_seconds=0.05)
        )

        await asyncio.wait_for(
            call_tool("ask_user", {"questions": [{"question": "Done?"}]}),
            timeout=10.0,
        )
        request = await responder

        assert not (ipc_dirs["responses"] / f"{request['request_id']}.json").exists()
