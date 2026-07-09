"""Tests for watchdog-based IPC service request/response."""

from __future__ import annotations

import asyncio
import errno
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
    """Redirect IPC_DIR, RESPONSES_DIR, and requests/ to temp dirs."""
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


async def _call_ipc_tool():
    return await call_tool("take_screenshot", {})


class TestWatchdogPicksUpResponse:
    """Watchdog detects the response file and unblocks the coroutine."""

    @pytest.mark.asyncio
    async def test_response_written_after_request(self, ipc_dirs: dict[str, Path]) -> None:
        """Write response 0.2s after request, verify it unblocks promptly."""
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

        write_patch = patch(
            "agent_runner.agent_tools._ipc_request.write_request_file",
            side_effect=capture_write,
        )
        with write_patch:

            async def write_response_after_delay() -> None:
                # Wait for the request to be written so we know the request_id
                for _ in range(50):
                    if captured_id:
                        break
                    await asyncio.sleep(0.02)
                assert captured_id, "request_id was never captured"
                await asyncio.sleep(0.1)
                _write_response(ipc_dirs["responses"], captured_id[0], result={"status": "ok"})

            task = asyncio.create_task(write_response_after_delay())
            result = await asyncio.wait_for(
                _call_ipc_tool(),
                timeout=10.0,
            )
            await task

        assert len(result) == 1
        response_data = json.loads(result[0].text)
        assert response_data == {"status": "ok"}


class TestWatchdogTimeout:
    """No response file appears, request times out."""

    @pytest.mark.asyncio
    async def test_timeout_returns_error(self, ipc_dirs: dict[str, Path]) -> None:
        with patch(
            "agent_runner.agent_tools._ipc_request._wait_for_response_file",
            side_effect=TimeoutError,
        ):
            result = await _call_ipc_tool()

        assert len(result) == 1
        assert "timed out" in result[0].text.lower()


class TestWatchdogFallback:
    """The response waiter still works when watchdog cannot allocate a watch."""

    @pytest.mark.asyncio
    async def test_inotify_limit_falls_back_to_polling(self, ipc_dirs: dict[str, Path]) -> None:
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

        with (
            patch(
                "agent_runner.agent_tools._ipc_request.write_request_file",
                side_effect=capture_write,
            ),
            patch("agent_runner.agent_tools._ipc_request.Observer") as observer_cls,
        ):
            observer_cls.return_value.start.side_effect = OSError(
                errno.ENOSPC, "inotify watch limit reached"
            )

            async def write_response_after_delay() -> None:
                for _ in range(50):
                    if captured_id:
                        break
                    await asyncio.sleep(0.02)
                assert captured_id
                await asyncio.sleep(0.05)
                _write_response(ipc_dirs["responses"], captured_id[0], result={"ok": True})

            task = asyncio.create_task(write_response_after_delay())
            result = await asyncio.wait_for(
                _call_ipc_tool(),
                timeout=10.0,
            )
            await task

        assert json.loads(result[0].text) == {"ok": True}


class TestResponseFileCleanedUp:
    """Response file is deleted after reading."""

    @pytest.mark.asyncio
    async def test_file_deleted(self, ipc_dirs: dict[str, Path]) -> None:
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

        write_patch = patch(
            "agent_runner.agent_tools._ipc_request.write_request_file",
            side_effect=capture_write,
        )
        with write_patch:

            async def write_response_after_delay() -> None:
                for _ in range(50):
                    if captured_id:
                        break
                    await asyncio.sleep(0.02)
                assert captured_id
                await asyncio.sleep(0.05)
                _write_response(ipc_dirs["responses"], captured_id[0], result={"cleaned": True})

            task = asyncio.create_task(write_response_after_delay())
            result = await asyncio.wait_for(
                _call_ipc_tool(),
                timeout=10.0,
            )
            await task

        # Verify result was read successfully
        assert len(result) == 1
        assert "cleaned" in result[0].text

        # Verify file was cleaned up
        response_file = ipc_dirs["responses"] / f"{captured_id[0]}.json"
        assert not response_file.exists()


class TestErrorResponse:
    """Error responses are returned correctly."""

    @pytest.mark.asyncio
    async def test_error_field_returned(self, ipc_dirs: dict[str, Path]) -> None:
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

        write_patch = patch(
            "agent_runner.agent_tools._ipc_request.write_request_file",
            side_effect=capture_write,
        )
        with write_patch:

            async def write_error_response() -> None:
                for _ in range(50):
                    if captured_id:
                        break
                    await asyncio.sleep(0.02)
                assert captured_id
                await asyncio.sleep(0.05)
                _write_response(ipc_dirs["responses"], captured_id[0], error="policy denied")

            task = asyncio.create_task(write_error_response())
            result = await asyncio.wait_for(
                _call_ipc_tool(),
                timeout=10.0,
            )
            await task

        assert len(result) == 1
        assert result[0].text == "Error: policy denied"
