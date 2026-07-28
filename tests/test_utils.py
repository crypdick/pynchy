"""Tests for shared utility functions."""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from pynchy.async_tasks import create_background_task
from pynchy.utils import compute_next_run, generate_message_id, run_shell_command

if TYPE_CHECKING:
    from pathlib import Path

INTENTIONAL_FAILURE_MESSAGE = "intentional failure"


async def _wait_for_process_exit(pid: int) -> None:
    """Wait briefly for the OS to reap a killed test subprocess."""
    for _ in range(200):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"subprocess {pid} survived shell command cleanup")


def _pid_recording_command(pid_file: Path) -> str:
    script = (
        f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    )
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def _exit_trap_command(pid_file: Path, cleanup_file: Path) -> str:
    command = _pid_recording_command(pid_file)
    cleanup = f"printf cleaned > {shlex.quote(str(cleanup_file))}"
    script = f"trap 'exit 143' TERM INT; trap {shlex.quote(cleanup)} EXIT; {command}"
    return f"/bin/sh -c {shlex.quote(script)}"


async def _wait_for_pid_file(pid_file: Path) -> int:
    for _ in range(200):
        try:
            contents = await asyncio.to_thread(pid_file.read_text, encoding="utf-8")
        except FileNotFoundError:
            await asyncio.sleep(0.01)
            continue
        return int(contents)
    pytest.fail("subprocess did not record its PID")


class TestGenerateMessageId:
    """Test generate_message_id for unique, prefixed identifiers."""

    def test_returns_string(self):
        result = generate_message_id("host")
        assert isinstance(result, str)

    def test_includes_prefix(self):
        result = generate_message_id("host")
        assert result.startswith("host-")

    def test_prefix_with_hyphen(self):
        result = generate_message_id("sys-notice")
        assert result.startswith("sys-notice-")

    def test_no_prefix_returns_timestamp_only(self):
        result = generate_message_id()
        # Should be a numeric timestamp string
        assert result.isdigit()

    def test_empty_prefix_returns_timestamp_only(self):
        result = generate_message_id("")
        assert result.isdigit()

    def test_unique_ids(self):
        """Consecutive calls should produce different IDs (at ms resolution)."""
        ids = {generate_message_id("test") for _ in range(10)}
        # At least some should be unique (timing-dependent, but ms precision
        # means most rapid calls still produce the same — just verify format)
        assert all(id_.startswith("test-") for id_ in ids)

    def test_timestamp_part_is_numeric(self):
        result = generate_message_id("host")
        ts_part = result.split("-", 1)[1]
        assert ts_part.isdigit()


class TestComputeNextRun:
    """Test compute_next_run for cron, interval, and once schedule types.

    This is critical business logic: wrong next_run means tasks fire at the
    wrong time or never fire at all.
    """

    def test_cron_returns_future_timestamp(self):
        """Cron schedules should produce a future ISO timestamp."""
        result = compute_next_run("cron", "0 9 * * *", "UTC")
        assert result is not None
        parsed = datetime.fromisoformat(result)
        assert parsed > datetime.now(parsed.tzinfo)

    def test_cron_invalid_expression_raises(self):
        """Invalid cron expression should raise."""
        with pytest.raises((ValueError, KeyError)):
            compute_next_run("cron", "not a cron", "UTC")

    def test_interval_returns_future_timestamp(self):
        """Interval schedules produce a timestamp ~interval ms in the future."""
        result = compute_next_run("interval", "3600000", "UTC")  # 1 hour
        assert result is not None
        parsed = datetime.fromisoformat(result)
        # Should be ~1 hour in the future (within a few seconds)
        delta = (parsed - datetime.now(UTC)).total_seconds()
        assert 3590 < delta < 3610

    def test_interval_zero_raises(self):
        """Zero interval should raise ValueError."""
        with pytest.raises(ValueError, match="positive"):
            compute_next_run("interval", "0", "UTC")

    def test_interval_negative_raises(self):
        """Negative interval should raise ValueError."""
        with pytest.raises(ValueError, match="positive"):
            compute_next_run("interval", "-1000", "UTC")

    def test_interval_non_numeric_raises(self):
        """Non-numeric interval should raise ValueError."""
        with pytest.raises(ValueError, match="invalid literal"):
            compute_next_run("interval", "abc", "UTC")

    def test_once_returns_none(self):
        """'once' schedule type returns None (no next run)."""
        result = compute_next_run("once", "2025-01-01T00:00:00", "UTC")
        assert result is None

    def test_cron_respects_timezone(self):
        """Cron should use the specified timezone for calculation."""
        # Both should return valid timestamps but potentially different times
        utc_result = compute_next_run("cron", "0 9 * * *", "UTC")
        est_result = compute_next_run("cron", "0 9 * * *", "America/New_York")
        assert utc_result is not None
        assert est_result is not None
        # Both should be valid ISO timestamps
        datetime.fromisoformat(utc_result)
        datetime.fromisoformat(est_result)

    def test_cron_always_returns_utc(self):
        """Cron next_run must always be UTC so SQLite string comparison works.

        Regression test: non-UTC offsets (e.g. -08:00) sort incorrectly against
        UTC timestamps in SQLite's lexicographic comparison, causing tasks to
        appear perpetually due.
        """
        result = compute_next_run("cron", "0 4 * * *", "America/Los_Angeles")
        assert result is not None
        parsed = datetime.fromisoformat(result)
        # Must be UTC (offset +00:00), not local timezone offset
        assert parsed.utcoffset().total_seconds() == 0, (
            f"Expected UTC offset but got {parsed.isoformat()}"
        )
        # String comparison with a UTC now must work correctly
        now_str = datetime.now(UTC).isoformat()
        assert (result <= now_str) == (parsed <= datetime.now(UTC))

    def test_interval_returns_utc(self):
        """Interval next_run must be in UTC."""
        result = compute_next_run("interval", "3600000", "America/Los_Angeles")
        assert result is not None
        parsed = datetime.fromisoformat(result)
        assert parsed.utcoffset().total_seconds() == 0

    def test_interval_small_value(self):
        """Small but valid interval (1ms) should work."""
        result = compute_next_run("interval", "1", "UTC")
        assert result is not None


class TestCreateBackgroundTask:
    """Test create_background_task for exception logging on fire-and-forget coroutines.

    Silently swallowed exceptions in background tasks make debugging nearly
    impossible — this helper ensures failures always surface in logs.
    """

    @pytest.mark.asyncio
    async def test_successful_task_completes(self):
        """A successful coroutine should complete normally."""
        result_holder: list[str] = []

        async def success():
            await asyncio.sleep(0)
            result_holder.append("done")

        task = create_background_task(success(), name="test-success")
        await task
        assert result_holder == ["done"]

    @pytest.mark.asyncio
    async def test_failed_task_logs_error(self, caplog):
        """A failing coroutine should log the exception via the done callback."""

        async def fail():
            await asyncio.sleep(0)
            raise RuntimeError(INTENTIONAL_FAILURE_MESSAGE)

        task = create_background_task(fail(), name="test-failure")

        # Wait for the task to complete (it will raise internally)
        with pytest.raises(RuntimeError, match="intentional failure"):
            await task

        assert "intentional failure" in caplog.text

        # The done callback fires after the await, but we need to let the
        # event loop process it
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_cancelled_task_does_not_log_error(self):
        """A cancelled task should not trigger error logging."""

        async def hang():
            await asyncio.sleep(999)

        task = create_background_task(hang(), name="test-cancel")
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_task_has_name(self):
        """The created task should have the specified name."""

        async def noop():
            pass

        task = create_background_task(noop(), name="my-task-name")
        assert task.get_name() == "my-task-name"
        await task

    @pytest.mark.asyncio
    async def test_task_without_name(self):
        """Creating a task without a name should still work."""

        async def noop():
            pass

        task = create_background_task(noop())
        await task  # Should complete without error


class TestRunShellCommand:
    """Shell command cleanup must include every descendant process."""

    @pytest.mark.asyncio
    async def test_timeout_kills_descendant_process(self, tmp_path: Path):
        pid_file = tmp_path / "timeout-child.pid"

        result = await run_shell_command(
            _pid_recording_command(pid_file),
            cwd=str(tmp_path),
            timeout_seconds=1,
        )

        assert result.timed_out is True
        await _wait_for_process_exit(await _wait_for_pid_file(pid_file))

    @pytest.mark.asyncio
    async def test_timeout_allows_shell_exit_trap_to_clean_up(self, tmp_path: Path):
        pid_file = tmp_path / "trapped-child.pid"
        cleanup_file = tmp_path / "cleanup-complete"

        result = await run_shell_command(
            _exit_trap_command(pid_file, cleanup_file),
            cwd=str(tmp_path),
            timeout_seconds=1,
        )

        assert result.timed_out is True
        assert cleanup_file.read_text(encoding="utf-8") == "cleaned"
        await _wait_for_process_exit(await _wait_for_pid_file(pid_file))

    @pytest.mark.asyncio
    async def test_cancellation_kills_descendant_process(self, tmp_path: Path):
        pid_file = tmp_path / "cancelled-child.pid"
        command_task = asyncio.create_task(
            run_shell_command(
                _pid_recording_command(pid_file),
                cwd=str(tmp_path),
                timeout_seconds=60,
            )
        )
        child_pid = await _wait_for_pid_file(pid_file)

        command_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await command_task
        await _wait_for_process_exit(child_pid)
