"""Tests for output file processing in the IPC watcher.

Covers: parsing and dispatching output events, file deletion after
processing, query-done pulse detection, and error handling.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.db import _init_test_database
from pynchy.ipc._watcher import _process_output_file
from pynchy.types import ContainerOutput


@pytest.fixture
async def _db():
    await _init_test_database()


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
    """Tests for _process_output_file — parsing, dispatch, and cleanup."""

    async def test_text_event_dispatched_to_handler(self, _db, tmp_path: Path):
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
        with patch("pynchy.ipc._watcher._get_output_handler", return_value=handler):
            await _process_output_file(file_path, "test-group", ipc_dir)

        handler.assert_called_once()
        output: ContainerOutput = handler.call_args[0][0]
        assert output.type == "text"
        assert output.text == "Hello world"
        assert output.status == "success"

    async def test_file_deleted_when_handler_exists(self, _db, tmp_path: Path):
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
        with patch("pynchy.ipc._watcher._get_output_handler", return_value=handler):
            await _process_output_file(file_path, "test-group", ipc_dir)

        assert not file_path.exists()

    async def test_file_preserved_when_no_handler(self, _db, tmp_path: Path):
        """Output files should be left in place when no session handler exists.

        One-shot containers (scheduled tasks) have no session, so the
        watcher must leave their output files for run_container_agent()
        to collect after the container exits.
        """
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

        with patch("pynchy.ipc._watcher._get_output_handler", return_value=None):
            await _process_output_file(file_path, "test-group", ipc_dir)

        assert file_path.exists(), "File should be preserved for one-shot container collection"

    async def test_thinking_event_dispatched(self, _db, tmp_path: Path):
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
        with patch("pynchy.ipc._watcher._get_output_handler", return_value=handler):
            await _process_output_file(file_path, "test-group", ipc_dir)

        output: ContainerOutput = handler.call_args[0][0]
        assert output.type == "thinking"
        assert output.thinking == "Let me consider..."

    async def test_tool_use_event_dispatched(self, _db, tmp_path: Path):
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
        with patch("pynchy.ipc._watcher._get_output_handler", return_value=handler):
            await _process_output_file(file_path, "test-group", ipc_dir)

        output: ContainerOutput = handler.call_args[0][0]
        assert output.type == "tool_use"
        assert output.tool_name == "bash"
        assert output.tool_input == {"command": "ls"}


# ---------------------------------------------------------------------------
# Query-done pulse detection
# ---------------------------------------------------------------------------


class TestQueryDonePulse:
    """Tests for detecting the query-done pulse in output files."""

    async def test_result_with_session_id_signals_query_done(self, _db, tmp_path: Path):
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
            },
        )

        handler = AsyncMock()
        with (
            patch("pynchy.ipc._watcher._get_output_handler", return_value=handler),
            patch("pynchy.ipc._watcher._signal_query_done") as mock_signal,
        ):
            await _process_output_file(file_path, "test-group", ipc_dir)

        mock_signal.assert_called_once_with("test-group")
        assert not file_path.exists()

    async def test_text_event_does_not_signal_query_done(self, _db, tmp_path: Path):
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

        with (
            patch("pynchy.ipc._watcher._get_output_handler", return_value=None),
            patch("pynchy.ipc._watcher._signal_query_done") as mock_signal,
        ):
            await _process_output_file(file_path, "test-group", ipc_dir)

        mock_signal.assert_not_called()

    async def test_result_with_error_does_not_signal_query_done(self, _db, tmp_path: Path):
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

        with (
            patch("pynchy.ipc._watcher._get_output_handler", return_value=None),
            patch("pynchy.ipc._watcher._signal_query_done") as mock_signal,
        ):
            await _process_output_file(file_path, "test-group", ipc_dir)

        # is_query_done_pulse requires error=None
        mock_signal.assert_not_called()

    async def test_result_with_text_result_does_not_signal_query_done(self, _db, tmp_path: Path):
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

        with (
            patch("pynchy.ipc._watcher._get_output_handler", return_value=None),
            patch("pynchy.ipc._watcher._signal_query_done") as mock_signal,
        ):
            await _process_output_file(file_path, "test-group", ipc_dir)

        # is_query_done_pulse requires result=None
        mock_signal.assert_not_called()

    async def test_handler_called_before_query_done_signal(self, _db, tmp_path: Path):
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

        handler = AsyncMock()
        with (
            patch("pynchy.ipc._watcher._get_output_handler", return_value=handler),
            patch("pynchy.ipc._watcher._signal_query_done"),
        ):
            await _process_output_file(file_path, "test-group", ipc_dir)

        # Handler should still have been called
        handler.assert_called_once()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestOutputFileErrors:
    """Tests for error handling during output file processing."""

    async def test_malformed_json_moved_to_errors(self, _db, tmp_path: Path):
        """A file with invalid JSON should be moved to errors/."""
        ipc_dir = tmp_path / "ipc"
        target_dir = ipc_dir / "test-group" / "output"
        target_dir.mkdir(parents=True)
        bad_file = target_dir / "bad.json"
        bad_file.write_text("not valid json {{{")

        await _process_output_file(bad_file, "test-group", ipc_dir)

        assert not bad_file.exists()
        assert (ipc_dir / "errors" / "test-group-bad.json").exists()

    async def test_missing_status_field_moved_to_errors(self, _db, tmp_path: Path):
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

        await _process_output_file(file_path, "test-group", ipc_dir)

        assert not file_path.exists()
        assert (ipc_dir / "errors" / "test-group-test.json").exists()

    async def test_handler_exception_does_not_prevent_file_deletion(self, _db, tmp_path: Path):
        """If the output handler raises, the file should still be deleted."""
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

        handler = AsyncMock(side_effect=RuntimeError("handler boom"))
        with patch("pynchy.ipc._watcher._get_output_handler", return_value=handler):
            await _process_output_file(file_path, "test-group", ipc_dir)

        # File should be deleted even though handler raised
        assert not file_path.exists()
        # Should NOT be in errors/ — the handler failure is non-fatal
        assert not (ipc_dir / "errors").exists()

    async def test_multiple_output_files_processed_in_order(self, _db, tmp_path: Path):
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
        with patch("pynchy.ipc._watcher._get_output_handler", return_value=handler):
            await _process_output_file(file1, "test-group", ipc_dir)
            await _process_output_file(file2, "test-group", ipc_dir)

        assert handler.call_count == 2
        texts = [call.args[0].text for call in handler.call_args_list]
        assert texts == ["first", "second"]
        assert not file1.exists()
        assert not file2.exists()
