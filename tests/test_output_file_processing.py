"""Tests for output file processing in the IPC watcher.

Covers: parsing and dispatching output events, file deletion after
processing, query-done pulse detection, and error handling.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import init_test_database

from pynchy.host.container_manager.ipc.output_processing import process_output_file

if TYPE_CHECKING:
    from pathlib import Path

    from pynchy.agent_protocol.api import ContainerOutput


@pytest.fixture
async def _db():
    await init_test_database()


def _write_output_file(base_dir: Path, group: str, data: dict, filename: str = "test.json") -> Path:
    """Helper to create an output file in the expected directory structure."""
    target_dir = base_dir / group / "output"
    target_dir.mkdir(parents=True, exist_ok=True)
    file_path = target_dir / filename
    file_path.write_text(json.dumps(data))
    return file_path


# ---------------------------------------------------------------------------
# Output file parsing and dispatch
# ---------------------------------------------------------------------------


class TestOutputFileProcessing:
    """Tests for process_output_file — parsing, dispatch, and cleanup."""

    pytestmark = pytest.mark.usefixtures("_db")

    async def test_text_event_dispatched_to_handler(self, tmp_path: Path):
        """A text output event should be dispatched to the output handler."""
        ipc_dir = tmp_path / "ipc"
        file_path = _write_output_file(
            ipc_dir,
            "test-group",
            {
                "status": "success",
                "type": "text",
                "text": "Hello world",
            },
        )

        handler = AsyncMock()
        with patch(
            "pynchy.host.container_manager.session.get_session_output_handler",
            return_value=handler,
        ):
            await process_output_file(file_path, "test-group", ipc_dir)

        handler.assert_called_once()
        output: ContainerOutput = handler.call_args[0][0]
        assert output.type == "text"
        assert output.text == "Hello world"
        assert output.status == "success"

    async def test_file_deleted_when_handler_exists(self, tmp_path: Path):
        """Output file should be unlinked after a handler consumes it."""
        ipc_dir = tmp_path / "ipc"
        file_path = _write_output_file(
            ipc_dir,
            "test-group",
            {
                "status": "success",
                "type": "text",
                "text": "will be deleted",
            },
        )

        handler = AsyncMock()
        with patch(
            "pynchy.host.container_manager.session.get_session_output_handler",
            return_value=handler,
        ):
            await process_output_file(file_path, "test-group", ipc_dir)

        assert not file_path.exists()

    async def test_file_preserved_when_no_handler(self, tmp_path: Path):
        """Output files should be left in place when no session handler exists."""
        ipc_dir = tmp_path / "ipc"
        file_path = _write_output_file(
            ipc_dir,
            "test-group",
            {
                "status": "success",
                "type": "text",
                "text": "one-shot output",
            },
        )

        with patch(
            "pynchy.host.container_manager.session.get_session_output_handler",
            return_value=None,
        ):
            await process_output_file(file_path, "test-group", ipc_dir)

        assert file_path.exists(), "File should be preserved for one-shot container collection"

    async def test_thinking_event_dispatched(self, tmp_path: Path):
        """Thinking events should be dispatched to the output handler."""
        ipc_dir = tmp_path / "ipc"
        file_path = _write_output_file(
            ipc_dir,
            "test-group",
            {
                "status": "success",
                "type": "thinking",
                "thinking": "Let me consider...",
            },
        )

        handler = AsyncMock()
        with patch(
            "pynchy.host.container_manager.session.get_session_output_handler",
            return_value=handler,
        ):
            await process_output_file(file_path, "test-group", ipc_dir)

        output: ContainerOutput = handler.call_args[0][0]
        assert output.type == "thinking"
        assert output.thinking == "Let me consider..."

    async def test_tool_use_event_dispatched(self, tmp_path: Path):
        """Tool use events should be dispatched to the output handler."""
        ipc_dir = tmp_path / "ipc"
        file_path = _write_output_file(
            ipc_dir,
            "test-group",
            {
                "status": "success",
                "type": "tool_use",
                "tool_name": "bash",
                "tool_input": {"command": "ls"},
            },
        )

        handler = AsyncMock()
        with patch(
            "pynchy.host.container_manager.session.get_session_output_handler",
            return_value=handler,
        ):
            await process_output_file(file_path, "test-group", ipc_dir)

        output: ContainerOutput = handler.call_args[0][0]
        assert output.type == "tool_use"
        assert output.tool_name == "bash"
        assert output.tool_input == {"command": "ls"}


# ---------------------------------------------------------------------------
# Query-done pulse detection
# ---------------------------------------------------------------------------


class TestQueryDonePulse:
    """Tests for detecting the query-done pulse in output files."""

    pytestmark = pytest.mark.usefixtures("_db")

    async def test_result_with_session_id_signals_query_done(self, tmp_path: Path):
        """A result event with new_session_id should signal query done."""
        ipc_dir = tmp_path / "ipc"
        file_path = _write_output_file(
            ipc_dir,
            "test-group",
            {
                "status": "success",
                "result": None,
                "new_session_id": "sess-abc123",
                "type": "result",
                "query_id": "query-result",
            },
        )

        handler = AsyncMock()
        session = MagicMock()
        with (
            patch(
                "pynchy.host.container_manager.session.get_session_output_handler",
                return_value=handler,
            ),
            patch("pynchy.host.container_manager.session.get_session", return_value=session),
        ):
            await process_output_file(file_path, "test-group", ipc_dir)

        session.signal_query_progress.assert_called_once_with("query-result")
        session.signal_query_done.assert_called_once_with("query-result")
        assert not file_path.exists()

    async def test_text_event_does_not_signal_query_done(self, tmp_path: Path):
        """A non-result event should not signal query done."""
        ipc_dir = tmp_path / "ipc"
        file_path = _write_output_file(
            ipc_dir,
            "test-group",
            {
                "status": "success",
                "type": "text",
                "text": "intermediate output",
            },
        )

        session = MagicMock()
        with (
            patch(
                "pynchy.host.container_manager.session.get_session_output_handler",
                return_value=None,
            ),
            patch("pynchy.host.container_manager.session.get_session", return_value=session),
        ):
            await process_output_file(file_path, "test-group", ipc_dir)

        session.signal_query_done.assert_not_called()

    async def test_result_with_error_does_not_signal_query_done(self, tmp_path: Path):
        """A result event with an error should not signal query done."""
        ipc_dir = tmp_path / "ipc"
        file_path = _write_output_file(
            ipc_dir,
            "test-group",
            {
                "status": "success",
                "result": None,
                "new_session_id": "sess-abc123",
                "error": "something went wrong",
                "type": "result",
            },
        )

        session = MagicMock()
        with (
            patch(
                "pynchy.host.container_manager.session.get_session_output_handler",
                return_value=None,
            ),
            patch("pynchy.host.container_manager.session.get_session", return_value=session),
        ):
            await process_output_file(file_path, "test-group", ipc_dir)

        # is_query_done_pulse requires error=None
        session.signal_query_done.assert_not_called()

    async def test_result_for_missing_session_does_not_fail_processing(self, tmp_path: Path):
        """A completion pulse can arrive after its session has already ended."""
        ipc_dir = tmp_path / "ipc"
        file_path = _write_output_file(
            ipc_dir,
            "test-group",
            {
                "status": "success",
                "result": None,
                "new_session_id": "sess-abc123",
                "type": "result",
                "query_id": "query-ended",
            },
        )

        with (
            patch(
                "pynchy.host.container_manager.session.get_session_output_handler",
                return_value=None,
            ),
            patch("pynchy.host.container_manager.session.get_session", return_value=None),
        ):
            await process_output_file(file_path, "test-group", ipc_dir)

        assert file_path.exists()

    async def test_result_with_text_result_does_not_signal_query_done(self, tmp_path: Path):
        """A result event with a non-None result should not signal query done."""
        ipc_dir = tmp_path / "ipc"
        file_path = _write_output_file(
            ipc_dir,
            "test-group",
            {
                "status": "success",
                "result": "some text result",
                "new_session_id": "sess-abc123",
                "type": "result",
            },
        )

        session = MagicMock()
        with (
            patch(
                "pynchy.host.container_manager.session.get_session_output_handler",
                return_value=None,
            ),
            patch("pynchy.host.container_manager.session.get_session", return_value=session),
        ):
            await process_output_file(file_path, "test-group", ipc_dir)

        # is_query_done_pulse requires result=None
        session.signal_query_done.assert_not_called()

    async def test_handler_called_before_query_done_signal(self, tmp_path: Path):
        """Handler should be called even for query-done pulse events."""
        ipc_dir = tmp_path / "ipc"
        file_path = _write_output_file(
            ipc_dir,
            "test-group",
            {
                "status": "success",
                "result": None,
                "new_session_id": "sess-abc123",
                "type": "result",
            },
        )

        observed: list[str] = []

        async def handler(_output: ContainerOutput) -> None:
            await asyncio.sleep(0)
            observed.append("handler")

        session = MagicMock()
        session.signal_query_done.side_effect = lambda _query_id: (
            observed.append("query-done") or True
        )
        with (
            patch(
                "pynchy.host.container_manager.session.get_session_output_handler",
                return_value=handler,
            ),
            patch("pynchy.host.container_manager.session.get_session", return_value=session),
        ):
            await process_output_file(file_path, "test-group", ipc_dir)

        assert observed == ["handler", "query-done"]

    async def test_progress_refreshes_before_slow_output_delivery(self, tmp_path: Path):
        """Internal tool activity counts before channel delivery can block."""
        ipc_dir = tmp_path / "ipc"
        file_path = _write_output_file(
            ipc_dir,
            "test-group",
            {
                "status": "success",
                "type": "tool_use",
                "tool_name": "exec_command",
                "tool_input": {"command": "git commit"},
                "query_id": "query-hooks",
            },
        )
        observed: list[str] = []

        async def handler(_output: ContainerOutput) -> None:
            await asyncio.sleep(0)
            observed.append("handler")

        session = MagicMock()
        session.output_handler = handler
        session.signal_query_progress.side_effect = lambda _query_id: (
            observed.append("progress") or True
        )
        with (
            patch(
                "pynchy.host.container_manager.session.get_session_output_handler",
                return_value=handler,
            ),
            patch("pynchy.host.container_manager.session.get_session", return_value=session),
        ):
            await process_output_file(file_path, "test-group", ipc_dir)

        assert observed == ["progress", "handler"]

    async def test_stale_prior_turn_output_is_discarded(self, tmp_path: Path):
        """A delayed prior-turn event cannot refresh or reach the current handler."""
        ipc_dir = tmp_path / "ipc"
        file_path = _write_output_file(
            ipc_dir,
            "test-group",
            {
                "status": "success",
                "type": "text",
                "text": "late prior output",
                "query_id": "query-prior",
            },
        )
        handler = AsyncMock()
        session = MagicMock()
        session.output_handler = handler
        session.signal_query_progress.return_value = False

        with (
            patch(
                "pynchy.host.container_manager.session.get_session_output_handler",
                return_value=handler,
            ),
            patch("pynchy.host.container_manager.session.get_session", return_value=session),
        ):
            await process_output_file(file_path, "test-group", ipc_dir)

        session.signal_query_progress.assert_called_once_with("query-prior")
        handler.assert_not_awaited()
        assert not file_path.exists()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestOutputFileErrors:
    """Tests for error handling during output file processing."""

    async def test_missing_file_is_idempotent(self, tmp_path: Path):
        """Duplicate watchdog/sweep delivery should not turn a consumed file into an error."""
        ipc_dir = tmp_path / "ipc"
        file_path = ipc_dir / "test-group" / "output" / "already-gone.json"

        await process_output_file(file_path, "test-group", ipc_dir)

        assert not (ipc_dir / "errors").exists()

    async def test_file_consumed_during_handler_is_idempotent(self, tmp_path: Path):
        """A competing output processor may delete the file after this processor read it."""
        ipc_dir = tmp_path / "ipc"
        file_path = _write_output_file(
            ipc_dir,
            "test-group",
            {
                "status": "success",
                "type": "text",
                "text": "already consumed",
            },
        )

        def consume_file(_output: ContainerOutput) -> None:
            file_path.unlink()

        handler = AsyncMock(side_effect=consume_file)
        with patch(
            "pynchy.host.container_manager.session.get_session_output_handler",
            return_value=handler,
        ):
            await process_output_file(file_path, "test-group", ipc_dir)

        handler.assert_called_once()
        assert not file_path.exists()
        assert not (ipc_dir / "errors").exists()

    async def test_concurrent_duplicate_output_file_is_processed_once(self, tmp_path: Path):
        """Watchdog and runtime sweep can both discover the same output file."""
        ipc_dir = tmp_path / "ipc"
        file_path = _write_output_file(
            ipc_dir,
            "test-group",
            {
                "status": "success",
                "type": "text",
                "text": "single delivery",
            },
        )

        async def slow_handler(_output: ContainerOutput) -> None:
            await asyncio.sleep(0.01)

        handler = AsyncMock(side_effect=slow_handler)
        with patch(
            "pynchy.host.container_manager.session.get_session_output_handler",
            return_value=handler,
        ):
            await asyncio.gather(
                process_output_file(file_path, "test-group", ipc_dir),
                process_output_file(file_path, "test-group", ipc_dir),
            )

        handler.assert_called_once()
        assert not file_path.exists()
        assert not (ipc_dir / "errors").exists()

    async def test_malformed_json_moved_to_errors(self, tmp_path: Path):
        """A file with invalid JSON should be moved to errors/."""
        ipc_dir = tmp_path / "ipc"
        target_dir = ipc_dir / "test-group" / "output"
        target_dir.mkdir(parents=True)
        bad_file = target_dir / "bad.json"
        bad_file.write_text("not valid json {{{")

        await process_output_file(bad_file, "test-group", ipc_dir)

        assert not bad_file.exists()
        assert (ipc_dir / "errors" / "test-group-bad.json").exists()

    async def test_missing_status_field_moved_to_errors(self, tmp_path: Path):
        """A file missing the required 'status' field should be moved to errors/."""
        ipc_dir = tmp_path / "ipc"
        file_path = _write_output_file(
            ipc_dir,
            "test-group",
            {
                "type": "text",
                "text": "no status field",
            },
        )

        await process_output_file(file_path, "test-group", ipc_dir)

        assert not file_path.exists()
        assert (ipc_dir / "errors" / "test-group-test.json").exists()

    async def test_malformed_output_is_deleted_when_error_directory_is_unavailable(
        self, tmp_path: Path
    ) -> None:
        """A malformed file cannot remain when its error directory is blocked."""
        ipc_dir = tmp_path / "ipc"
        file_path = _write_output_file(ipc_dir, "test-group", {"type": "text"})
        (ipc_dir / "errors").write_text("blocked")

        await process_output_file(file_path, "test-group", ipc_dir)

        assert not file_path.exists()

    async def test_handler_exception_keeps_file_for_runtime_retry(self, tmp_path: Path):
        ipc_dir = tmp_path / "ipc"
        file_path = _write_output_file(
            ipc_dir,
            "test-group",
            {
                "status": "success",
                "type": "text",
                "text": "handler will fail",
            },
        )

        handler = AsyncMock(side_effect=[RuntimeError("handler boom"), None])
        with patch(
            "pynchy.host.container_manager.session.get_session_output_handler",
            return_value=handler,
        ):
            await process_output_file(file_path, "test-group", ipc_dir)
            assert file_path.exists()
            await process_output_file(file_path, "test-group", ipc_dir)

        assert not file_path.exists()
        assert handler.await_count == 2
        assert not (ipc_dir / "errors").exists()

    async def test_multiple_output_files_processed_in_order(self, tmp_path: Path):
        """Multiple output files should be processable independently."""
        ipc_dir = tmp_path / "ipc"
        file1 = _write_output_file(
            ipc_dir,
            "test-group",
            {"status": "success", "type": "text", "text": "first"},
            filename="001.json",
        )
        file2 = _write_output_file(
            ipc_dir,
            "test-group",
            {"status": "success", "type": "text", "text": "second"},
            filename="002.json",
        )

        handler = AsyncMock()
        with patch(
            "pynchy.host.container_manager.session.get_session_output_handler",
            return_value=handler,
        ):
            await process_output_file(file1, "test-group", ipc_dir)
            await process_output_file(file2, "test-group", ipc_dir)

        assert handler.call_count == 2
        texts = [call.args[0].text for call in handler.call_args_list]
        assert texts == ["first", "second"]
        assert not file1.exists()
        assert not file2.exists()
