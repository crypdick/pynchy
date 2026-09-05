"""Tests for watchdog-based IPC input waiting."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from agent_runner.ipc import wait_for_ipc_followup

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def input_dir(tmp_path: Path) -> Path:
    """Create and return a temporary IPC input directory."""
    d = tmp_path / "input"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def _patch_ipc_dirs(input_dir: Path) -> None:
    """Redirect IPC_INPUT_DIR and the close sentinel to temp dirs."""
    with (
        patch("agent_runner.ipc.IPC_INPUT_DIR", input_dir),
        patch("agent_runner.ipc.IPC_INPUT_CLOSE_SENTINEL", input_dir / "_close"),
    ):
        yield


def _write_message(input_dir: Path, text: str, *, index: int = 0) -> None:
    """Write a JSON message file to the input directory (atomic rename)."""
    data = {"type": "message", "text": text}
    final = input_dir / f"{index:06d}.json"
    tmp = final.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data))
    tmp.rename(final)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReturnsMessageOnJsonFile:
    """IPC follow-up waiting returns content when a JSON file appears."""

    @pytest.mark.asyncio
    async def test_single_message(self, input_dir: Path) -> None:
        async def write_after_delay() -> None:
            await asyncio.sleep(0.1)
            _write_message(input_dir, "hello world")

        writer_task = asyncio.create_task(write_after_delay())
        result = await asyncio.wait_for(wait_for_ipc_followup(), timeout=5.0)
        assert result is not None
        assert result.text == "hello world"
        await writer_task

    @pytest.mark.asyncio
    async def test_message_already_present(self, input_dir: Path) -> None:
        """If a message file is already present before waiting, it's picked up."""
        _write_message(input_dir, "pre-existing")
        # The initial sweep should catch it immediately
        # but watchdog needs the observer running first. We give a small window.
        result = await asyncio.wait_for(wait_for_ipc_followup(), timeout=5.0)
        assert result is not None
        assert result.text == "pre-existing"


class TestReturnsNoneOnClose:
    """IPC follow-up waiting returns None when _close sentinel appears."""

    @pytest.mark.asyncio
    async def test_close_sentinel(self, input_dir: Path) -> None:
        async def close_after_delay() -> None:
            await asyncio.sleep(0.1)
            (input_dir / "_close").touch()

        closer_task = asyncio.create_task(close_after_delay())
        result = await asyncio.wait_for(wait_for_ipc_followup(), timeout=5.0)
        assert result is None
        await closer_task

    @pytest.mark.asyncio
    async def test_close_sentinel_already_present(self, input_dir: Path) -> None:
        """If the _close sentinel already exists, returns None immediately."""
        (input_dir / "_close").touch()
        result = await asyncio.wait_for(wait_for_ipc_followup(), timeout=5.0)
        assert result is None


class TestDrainsMultipleMessages:
    """Multiple messages in the input dir are joined with newlines."""

    @pytest.mark.asyncio
    async def test_drains_multiple(self, input_dir: Path) -> None:
        async def write_batch_after_delay() -> None:
            await asyncio.sleep(0.1)
            _write_message(input_dir, "first", index=0)
            _write_message(input_dir, "second", index=1)
            _write_message(input_dir, "third", index=2)

        writer_task = asyncio.create_task(write_batch_after_delay())
        result = await asyncio.wait_for(wait_for_ipc_followup(), timeout=5.0)
        assert result is not None
        parts = result.text.split("\n")
        # All three messages should be present (drain reads all .json files)
        assert "first" in parts
        assert "second" in parts
        assert "third" in parts
        await writer_task
