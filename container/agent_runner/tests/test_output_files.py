"""Tests for file-based IPC output (write_output)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_runner.main import write_output
from agent_runner.models import ContainerOutput


@pytest.fixture()
def output_dir(tmp_path: Path) -> Path:
    """Patch IPC_OUTPUT_DIR to a temporary directory and return it."""
    # Don't pre-create — write_output should handle mkdir itself.
    d = tmp_path / "output"
    return d


@pytest.fixture(autouse=True)
def _patch_output_dir(output_dir: Path) -> None:
    """Redirect all write_output calls to the temp output dir."""
    with patch("agent_runner.main.IPC_OUTPUT_DIR", output_dir):
        yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWriteOutputCreatesFile:
    """write_output creates a .json file in the output directory."""

    def test_creates_json_file(self, output_dir: Path) -> None:
        output = ContainerOutput(status="success", type="text", text="hello")
        write_output(output)

        files = list(output_dir.glob("*.json"))
        assert len(files) == 1
        assert files[0].suffix == ".json"

    def test_file_content_matches_to_dict(self, output_dir: Path) -> None:
        output = ContainerOutput(status="success", type="text", text="hello")
        write_output(output)

        files = list(output_dir.glob("*.json"))
        content = json.loads(files[0].read_text())
        assert content == output.to_dict()

    def test_creates_output_dir_if_missing(self, output_dir: Path) -> None:
        assert not output_dir.exists()
        write_output(ContainerOutput(status="success", type="text", text="x"))
        assert output_dir.is_dir()

    def test_result_event_serialization(self, output_dir: Path) -> None:
        output = ContainerOutput(
            status="error",
            type="result",
            result="something went wrong",
            error="something went wrong",
            new_session_id="sess-123",
        )
        write_output(output)

        files = list(output_dir.glob("*.json"))
        content = json.loads(files[0].read_text())
        assert content == output.to_dict()
        assert content["error"] == "something went wrong"
        assert content["new_session_id"] == "sess-123"

    def test_tool_use_event_serialization(self, output_dir: Path) -> None:
        output = ContainerOutput(
            status="success",
            type="tool_use",
            tool_name="read_file",
            tool_input={"path": "/tmp/foo"},
        )
        write_output(output)

        files = list(output_dir.glob("*.json"))
        content = json.loads(files[0].read_text())
        assert content == output.to_dict()

    def test_thinking_event_serialization(self, output_dir: Path) -> None:
        output = ContainerOutput(
            status="success",
            type="thinking",
            thinking="Let me think about this...",
        )
        write_output(output)

        files = list(output_dir.glob("*.json"))
        content = json.loads(files[0].read_text())
        assert content == output.to_dict()


class TestSequentialOrdering:
    """Sequential calls produce files that sort correctly by filename."""

    def test_filenames_sort_chronologically(self, output_dir: Path) -> None:
        for i in range(5):
            write_output(ContainerOutput(status="success", type="text", text=f"msg-{i}"))

        files = sorted(output_dir.glob("*.json"))
        assert len(files) == 5

        # Verify content order matches write order
        for i, f in enumerate(files):
            content = json.loads(f.read_text())
            assert content["text"] == f"msg-{i}"

    def test_filenames_are_numeric(self, output_dir: Path) -> None:
        write_output(ContainerOutput(status="success", type="text", text="a"))
        write_output(ContainerOutput(status="success", type="text", text="b"))

        files = list(output_dir.glob("*.json"))
        for f in files:
            # Stem should be a valid integer (monotonic_ns)
            int(f.stem)  # Raises ValueError if not numeric

    def test_filenames_are_strictly_increasing(self, output_dir: Path) -> None:
        for _ in range(3):
            write_output(ContainerOutput(status="success", type="text", text="x"))

        stems = sorted(int(f.stem) for f in output_dir.glob("*.json"))
        assert len(stems) == 3
        for a, b in zip(stems, stems[1:], strict=False):
            assert a < b, f"Timestamps not strictly increasing: {a} >= {b}"


class TestAtomicWrite:
    """Atomic write pattern: no .tmp files left behind."""

    def test_no_tmp_files_remain(self, output_dir: Path) -> None:
        write_output(ContainerOutput(status="success", type="text", text="clean"))

        tmp_files = list(output_dir.glob("*.tmp"))
        assert tmp_files == [], f"Leftover tmp files: {tmp_files}"

    def test_no_tmp_files_after_multiple_writes(self, output_dir: Path) -> None:
        for i in range(10):
            write_output(ContainerOutput(status="success", type="text", text=f"msg-{i}"))

        tmp_files = list(output_dir.glob("*.tmp"))
        assert tmp_files == []
        json_files = list(output_dir.glob("*.json"))
        assert len(json_files) == 10
