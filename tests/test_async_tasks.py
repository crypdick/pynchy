"""Tests for background task lifecycle handling."""

from __future__ import annotations

import asyncio

import pytest

from pynchy.async_tasks import create_background_task

INTENTIONAL_FAILURE_MESSAGE = "intentional failure"


class TestCreateBackgroundTask:
    """Test exception logging on fire-and-forget coroutines."""

    @pytest.mark.asyncio
    async def test_successful_task_completes(self):
        result_holder: list[str] = []

        async def success():
            await asyncio.sleep(0)
            result_holder.append("done")

        task = create_background_task(success(), name="test-success")
        await task
        assert result_holder == ["done"]

    @pytest.mark.asyncio
    async def test_failed_task_logs_error(self, caplog):
        async def fail():
            await asyncio.sleep(0)
            raise RuntimeError(INTENTIONAL_FAILURE_MESSAGE)

        task = create_background_task(fail(), name="test-failure")
        with pytest.raises(RuntimeError, match="intentional failure"):
            await task
        assert "intentional failure" in caplog.text
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_cancelled_task_does_not_log_error(self):
        async def hang():
            await asyncio.sleep(999)

        task = create_background_task(hang(), name="test-cancel")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_task_has_name(self):
        async def noop():
            pass

        task = create_background_task(noop(), name="my-task-name")
        assert task.get_name() == "my-task-name"
        await task

    @pytest.mark.asyncio
    async def test_task_without_name(self):
        async def noop():
            pass

        task = create_background_task(noop())
        await task
