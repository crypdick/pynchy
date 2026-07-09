"""Tests for the ask_user MCP tool."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from agent_runner.agent_tools import call_tool

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def ipc_dirs(tmp_path: Path) -> dict[str, Path]:
    """Create temporary IPC directories and return them."""
    responses = tmp_path / "responses"
    responses.mkdir()
    requests = tmp_path / "requests"
    requests.mkdir()
    return {"responses": responses, "requests": requests, "ipc": tmp_path}


@pytest.fixture(autouse=True)
def _patch_ipc_dirs(ipc_dirs: dict[str, Path]):
    """Redirect IPC_DIR and RESPONSES_DIR to temp dirs."""
    with (
        patch("agent_runner.agent_tools._ipc_request.IPC_DIR", ipc_dirs["ipc"]),
        patch("agent_runner.agent_tools._ipc_request.RESPONSES_DIR", ipc_dirs["responses"]),
        patch("agent_runner.agent_tools._ipc_request.write_request_file"),
    ):
        yield


def _write_response(
    responses_dir: Path,
    request_id: str,
    *,
    result: dict | None = None,
    error: str | None = None,
) -> None:
    """Write a response file atomically (tmp -> rename), matching host behavior."""
    data: dict = {}
    if error:
        data["error"] = error
    if result is not None:
        data["result"] = result

    final = responses_dir / f"{request_id}.json"
    tmp = final.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data))
    tmp.rename(final)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAskUserIPCRequest:
    """The IPC helper writes the correct request and returns the response."""

    @pytest.mark.asyncio
    async def test_sends_correct_type_and_payload(self, ipc_dirs: dict[str, Path]) -> None:
        """Verify the request has kind 'ask_user:ask' and the questions payload."""
        captured_data: list[dict] = []

        def capture_write(
            kind: str,
            payload: dict,
            *,
            request_id: str | None = None,
            reply_to: str | None = "responses",
            deadline: str | None = None,
        ) -> tuple[str, str]:
            assert request_id is not None
            captured_data.append(
                {
                    "kind": kind,
                    "payload": payload,
                    "request_id": request_id,
                    "reply_to": reply_to,
                    "deadline": deadline,
                }
            )
            # Immediately write a response so the request unblocks
            _write_response(
                ipc_dirs["responses"],
                request_id,
                result={"answers": ["yes"]},
            )
            return "fake.json", request_id

        with patch(
            "agent_runner.agent_tools._ipc_request.write_request_file",
            side_effect=capture_write,
        ):
            questions = [{"question": "Continue?"}]
            await asyncio.wait_for(
                call_tool("ask_user", {"questions": questions}),
                timeout=10.0,
            )

        assert len(captured_data) == 1
        request = captured_data[0]
        assert request["kind"] == "ask_user:ask"
        assert request["payload"]["questions"] == [{"question": "Continue?"}]
        assert "request_id" in request

    @pytest.mark.asyncio
    async def test_returns_answer(self, ipc_dirs: dict[str, Path]) -> None:
        """Verify the tool returns the user's answer from the response file."""
        captured_id: list[str] = []

        def capture_write(
            kind: str,
            payload: dict,
            *,
            request_id: str | None = None,
            reply_to: str | None = "responses",
            deadline: str | None = None,
        ) -> tuple[str, str]:
            assert request_id is not None
            captured_id.append(request_id)
            return "fake.json", request_id

        with patch(
            "agent_runner.agent_tools._ipc_request.write_request_file",
            side_effect=capture_write,
        ):

            async def write_response_after_delay() -> None:
                for _ in range(50):
                    if captured_id:
                        break
                    await asyncio.sleep(0.02)
                assert captured_id, "request_id was never captured"
                await asyncio.sleep(0.1)
                _write_response(
                    ipc_dirs["responses"],
                    captured_id[0],
                    result={"answers": [{"text": "yes, go ahead"}]},
                )

            task = asyncio.create_task(write_response_after_delay())
            result = await asyncio.wait_for(
                call_tool("ask_user", {"questions": [{"question": "Should I proceed?"}]}),
                timeout=10.0,
            )
            await task

        assert len(result) == 1
        response_data = json.loads(result[0].text)
        assert response_data["answers"] == [{"text": "yes, go ahead"}]

    @pytest.mark.asyncio
    async def test_timeout_returns_error(
        self,
        ipc_dirs: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify timeout produces a descriptive error."""
        monkeypatch.setattr("agent_runner.agent_tools._tools_ask_user.ASK_USER_TIMEOUT", 1.0)

        result = await call_tool("ask_user", {"questions": [{"question": "Hello?"}]})

        assert len(result) == 1
        assert "timed out" in result[0].text.lower()

    @pytest.mark.asyncio
    async def test_questions_with_options(self, ipc_dirs: dict[str, Path]) -> None:
        """Verify questions with options are passed through correctly."""
        captured_data: list[dict] = []

        def capture_write(
            kind: str,
            payload: dict,
            *,
            request_id: str | None = None,
            reply_to: str | None = "responses",
            deadline: str | None = None,
        ) -> tuple[str, str]:
            assert request_id is not None
            captured_data.append({"kind": kind, "payload": payload, "request_id": request_id})
            _write_response(
                ipc_dirs["responses"],
                request_id,
                result={"answers": ["Option A"]},
            )
            return "fake.json", request_id

        with patch(
            "agent_runner.agent_tools._ipc_request.write_request_file",
            side_effect=capture_write,
        ):
            questions = [
                {
                    "question": "Which option?",
                    "options": [
                        {"label": "Option A", "description": "First choice"},
                        {"label": "Option B", "description": "Second choice"},
                    ],
                }
            ]
            await asyncio.wait_for(
                call_tool("ask_user", {"questions": questions}),
                timeout=10.0,
            )

        assert captured_data[0]["kind"] == "ask_user:ask"
        assert captured_data[0]["payload"]["questions"] == questions


class TestAskUserHandler:
    """The MCP tool handler validates input before calling the IPC helper."""

    @pytest.mark.asyncio
    async def test_empty_questions_returns_error(self) -> None:
        """Empty questions list should return an error without making an IPC call."""
        result = await call_tool("ask_user", {"questions": []})
        assert result.isError is True
        assert "non-empty" in result.content[0].text.lower()

    @pytest.mark.asyncio
    async def test_missing_questions_returns_error(self) -> None:
        """Missing questions key should return an error."""
        result = await call_tool("ask_user", {})
        assert result.isError is True
        assert "non-empty" in result.content[0].text.lower()

    @pytest.mark.asyncio
    async def test_handler_calls_ipc(self, ipc_dirs: dict[str, Path]) -> None:
        """Handler forwards questions to the IPC helper."""
        captured_data: list[dict] = []

        def capture_write(
            kind: str,
            payload: dict,
            *,
            request_id: str | None = None,
            reply_to: str | None = "responses",
            deadline: str | None = None,
        ) -> tuple[str, str]:
            assert request_id is not None
            captured_data.append({"kind": kind, "payload": payload, "request_id": request_id})
            _write_response(
                ipc_dirs["responses"],
                request_id,
                result={"answers": ["42"]},
            )
            return "fake.json", request_id

        with patch(
            "agent_runner.agent_tools._ipc_request.write_request_file",
            side_effect=capture_write,
        ):
            result = await asyncio.wait_for(
                call_tool("ask_user", {"questions": [{"question": "What is the answer?"}]}),
                timeout=10.0,
            )

        assert len(result) == 1
        assert "42" in result[0].text


class TestAskUserErrorResponse:
    """Host returns an error in the response file."""

    @pytest.mark.asyncio
    async def test_error_propagated(self, ipc_dirs: dict[str, Path]) -> None:
        """Error responses from the host are surfaced to the agent."""
        captured_id: list[str] = []

        def capture_write(
            kind: str,
            payload: dict,
            *,
            request_id: str | None = None,
            reply_to: str | None = "responses",
            deadline: str | None = None,
        ) -> tuple[str, str]:
            assert request_id is not None
            captured_id.append(request_id)
            return "fake.json", request_id

        with patch(
            "agent_runner.agent_tools._ipc_request.write_request_file",
            side_effect=capture_write,
        ):

            async def write_error_response() -> None:
                for _ in range(50):
                    if captured_id:
                        break
                    await asyncio.sleep(0.02)
                assert captured_id
                await asyncio.sleep(0.05)
                _write_response(
                    ipc_dirs["responses"],
                    captured_id[0],
                    error="channel unavailable",
                )

            task = asyncio.create_task(write_error_response())
            result = await asyncio.wait_for(
                call_tool(
                    "ask_user",
                    {"questions": [{"question": "Hello?"}]},
                ),
                timeout=10.0,
            )
            await task

        assert len(result) == 1
        assert "channel unavailable" in result[0].text


class TestResponseFileCleanup:
    """Response file is deleted after reading."""

    @pytest.mark.asyncio
    async def test_file_deleted_after_read(self, ipc_dirs: dict[str, Path]) -> None:
        captured_id: list[str] = []

        def capture_write(
            kind: str,
            payload: dict,
            *,
            request_id: str | None = None,
            reply_to: str | None = "responses",
            deadline: str | None = None,
        ) -> tuple[str, str]:
            assert request_id is not None
            captured_id.append(request_id)
            return "fake.json", request_id

        with patch(
            "agent_runner.agent_tools._ipc_request.write_request_file",
            side_effect=capture_write,
        ):

            async def write_response_after_delay() -> None:
                for _ in range(50):
                    if captured_id:
                        break
                    await asyncio.sleep(0.02)
                assert captured_id
                await asyncio.sleep(0.05)
                _write_response(
                    ipc_dirs["responses"],
                    captured_id[0],
                    result={"answers": ["done"]},
                )

            task = asyncio.create_task(write_response_after_delay())
            await asyncio.wait_for(
                call_tool("ask_user", {"questions": [{"question": "Done?"}]}),
                timeout=10.0,
            )
            await task

        response_file = ipc_dirs["responses"] / f"{captured_id[0]}.json"
        assert not response_file.exists()
