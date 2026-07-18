"""Behavioral tests for public IPC service requests."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from agent_runner.agent_tools import (
    AgentToolRuntime,
    request_host_service,
    use_agent_tool_runtime,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def ipc_dirs(tmp_path: Path) -> dict[str, Path]:
    """Create temporary directories for one request/response exchange."""
    responses = tmp_path / "responses"
    responses.mkdir()
    return {"responses": responses, "requests": tmp_path / "requests", "ipc": tmp_path}


@pytest.fixture(autouse=True)
def agent_tool_runtime(ipc_dirs: dict[str, Path]):
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


def _write_response(responses_dir: Path, request_id: str, *, result: dict | None = None) -> None:
    data = {"result": result} if result is not None else {}
    final = responses_dir / f"{request_id}.json"
    temporary = final.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data))
    temporary.rename(final)


async def _respond_to_request(
    ipc_dirs: dict[str, Path],
    result: dict,
    *,
    delay_seconds: float = 0.0,
) -> dict:
    for _ in range(50):
        request_files = list(ipc_dirs["requests"].glob("*.json"))
        if request_files:
            request = json.loads(request_files[0].read_text(encoding="utf-8"))
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
            _write_response(ipc_dirs["responses"], request["request_id"], result=result)
            return request
        await asyncio.sleep(0.02)
    raise AssertionError("public request_host_service never wrote a request")


class TestIpcServiceRequest:
    """The public request API handles response, timeout, and cleanup behavior."""

    @pytest.mark.asyncio
    async def test_response_written_after_request(self, ipc_dirs: dict[str, Path]) -> None:
        responder = asyncio.create_task(
            _respond_to_request(ipc_dirs, {"status": "ok"}, delay_seconds=0.1)
        )

        result = await asyncio.wait_for(request_host_service("screenshot", {}), timeout=10.0)
        request = await responder

        assert json.loads(result[0].text) == {"status": "ok"}
        assert request["kind"] == "service:screenshot"

    @pytest.mark.asyncio
    async def test_timeout_returns_error(self, ipc_dirs: dict[str, Path]) -> None:
        runtime = AgentToolRuntime(
            chat_jid="test@g.us",
            group_folder="test-group",
            is_admin=False,
            is_scheduled_task=False,
            ipc_dir=ipc_dirs["ipc"],
            service_request_timeout_seconds=0.01,
        )
        with use_agent_tool_runtime(runtime):
            result = await request_host_service("screenshot", {})

        assert "timed out" in result[0].text.lower()

    @pytest.mark.asyncio
    async def test_response_file_is_deleted_after_read(self, ipc_dirs: dict[str, Path]) -> None:
        responder = asyncio.create_task(
            _respond_to_request(ipc_dirs, {"cleaned": True}, delay_seconds=0.05)
        )

        result = await asyncio.wait_for(request_host_service("screenshot", {}), timeout=10.0)
        request = await responder

        assert "cleaned" in result[0].text
        assert not (ipc_dirs["responses"] / f"{request['request_id']}.json").exists()

    @pytest.mark.asyncio
    async def test_error_field_is_returned(self, ipc_dirs: dict[str, Path]) -> None:
        async def respond_with_error() -> None:
            for _ in range(50):
                request_files = list(ipc_dirs["requests"].glob("*.json"))
                if request_files:
                    request = json.loads(request_files[0].read_text(encoding="utf-8"))
                    final = ipc_dirs["responses"] / f"{request['request_id']}.json"
                    final.write_text(json.dumps({"error": "policy denied"}))
                    return
                await asyncio.sleep(0.02)
            raise AssertionError("public request_host_service never wrote a request")

        responder = asyncio.create_task(respond_with_error())
        result = await asyncio.wait_for(request_host_service("screenshot", {}), timeout=10.0)
        await responder

        assert result[0].text == "Error: policy denied"
