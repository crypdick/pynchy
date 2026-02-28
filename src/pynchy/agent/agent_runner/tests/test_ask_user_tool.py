"""Tests for the ask_user MCP tool."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_runner.agent_tools._tools_ask_user import _ask_user_handle


@pytest.fixture()
def ipc_dirs(tmp_path: Path) -> dict[str, Path]:
    """Create temporary IPC directories and return them."""
    responses = tmp_path / "responses"
    responses.mkdir()
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    return {"responses": responses, "tasks": tasks, "ipc": tmp_path}


@pytest.fixture(autouse=True)
def _patch_ipc_dirs(ipc_dirs: dict[str, Path]):
    """Redirect IPC_DIR and RESPONSES_DIR to temp dirs."""
    with (
        patch("agent_runner.agent_tools._ipc_request.IPC_DIR", ipc_dirs["ipc"]),
        patch("agent_runner.agent_tools._ipc_request.RESPONSES_DIR", ipc_dirs["responses"]),
        patch("agent_runner.agent_tools._ipc_request.write_ipc_file"),
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
    """The IPC helper writes the correct task and returns the response."""

    @pytest.mark.asyncio
    async def test_sends_correct_type_and_payload(self, ipc_dirs: dict[str, Path]) -> None:
        """Verify the task file has type 'ask_user:ask' and the questions payload."""
        captured_data: list[dict] = []

        def capture_write(directory: Path, data: dict) -> str:
            captured_data.append(data)
            # Immediately write a response so the request unblocks
            _write_response(
                ipc_dirs["responses"],
                data["request_id"],
                result={"answers": ["yes"]},
            )
            return "fake.json"

        with patch(
            "agent_runner.agent_tools._ipc_request.write_ipc_file",
            side_effect=capture_write,
        ):
            questions = [{"question": "Continue?"}]
            await asyncio.wait_for(
                _ask_user_handle({"questions": questions}),
                timeout=10.0,
            )

        assert len(captured_data) == 1
        task = captured_data[0]
        assert task["type"] == "ask_user:ask"
        assert task["questions"] == [{"question": "Continue?"}]
        assert "request_id" in task

    @pytest.mark.asyncio
    async def test_returns_answer(self, ipc_dirs: dict[str, Path]) -> None:
        """Verify the tool returns the user's answer from the response file."""
        captured_id: list[str] = []

        def capture_write(directory: Path, data: dict) -> str:
            captured_id.append(data["request_id"])
            return "fake.json"

        with patch(
            "agent_runner.agent_tools._ipc_request.write_ipc_file",
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
                _ask_user_handle({"questions": [{"question": "Should I proceed?"}]}),
                timeout=10.0,
            )
            await task

        assert len(result) == 1
        response_data = json.loads(result[0].text)
        assert response_data["answers"] == [{"text": "yes, go ahead"}]

    @pytest.mark.asyncio
    async def test_timeout_returns_error(self, ipc_dirs: dict[str, Path]) -> None:
        """Verify timeout produces a descriptive error."""
        from agent_runner.agent_tools._ipc_request import ipc_service_request

        result = await ipc_service_request(
            "ask_user",
            {"questions": [{"question": "Hello?"}]},
            timeout=1.0,
            type_override="ask_user:ask",
        )

        assert len(result) == 1
        assert "timed out" in result[0].text.lower()

    @pytest.mark.asyncio
    async def test_questions_with_options(self, ipc_dirs: dict[str, Path]) -> None:
        """Verify questions with options are passed through correctly."""
        captured_data: list[dict] = []

        def capture_write(directory: Path, data: dict) -> str:
            captured_data.append(data)
            _write_response(
                ipc_dirs["responses"],
                data["request_id"],
                result={"answers": ["Option A"]},
            )
            return "fake.json"

        with patch(
            "agent_runner.agent_tools._ipc_request.write_ipc_file",
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
                _ask_user_handle({"questions": questions}),
                timeout=10.0,
            )

        assert captured_data[0]["questions"] == questions


class TestAskUserHandler:
    """The MCP tool handler validates input before calling the IPC helper."""

    @pytest.mark.asyncio
    async def test_empty_questions_returns_error(self) -> None:
        """Empty questions list should return an error without making an IPC call."""
        result = await _ask_user_handle({"questions": []})
        assert result.isError is True
        assert "non-empty" in result.content[0].text.lower()

    @pytest.mark.asyncio
    async def test_missing_questions_returns_error(self) -> None:
        """Missing questions key should return an error."""
        result = await _ask_user_handle({})
        assert result.isError is True
        assert "non-empty" in result.content[0].text.lower()

    @pytest.mark.asyncio
    async def test_handler_calls_ipc(self, ipc_dirs: dict[str, Path]) -> None:
        """Handler forwards questions to the IPC helper."""
        captured_data: list[dict] = []

        def capture_write(directory: Path, data: dict) -> str:
            captured_data.append(data)
            _write_response(
                ipc_dirs["responses"],
                data["request_id"],
                result={"answers": ["42"]},
            )
            return "fake.json"

        with patch(
            "agent_runner.agent_tools._ipc_request.write_ipc_file",
            side_effect=capture_write,
        ):
            result = await asyncio.wait_for(
                _ask_user_handle({"questions": [{"question": "What is the answer?"}]}),
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

        def capture_write(directory: Path, data: dict) -> str:
            captured_id.append(data["request_id"])
            return "fake.json"

        with patch(
            "agent_runner.agent_tools._ipc_request.write_ipc_file",
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
                _ask_user_handle(
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

        def capture_write(directory: Path, data: dict) -> str:
            captured_id.append(data["request_id"])
            return "fake.json"

        with patch(
            "agent_runner.agent_tools._ipc_request.write_ipc_file",
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
                _ask_user_handle({"questions": [{"question": "Done?"}]}),
                timeout=10.0,
            )
            await task

        response_file = ipc_dirs["responses"] / f"{captured_id[0]}.json"
        assert not response_file.exists()
