"""Process fakes shared by direct host-runner tests."""

from __future__ import annotations

import asyncio


class FakeStdin:
    def __init__(self) -> None:
        self.buffer = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buffer += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class FakeStdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __aiter__(self) -> FakeStdout:
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class TimedStdout:
    def __init__(self, lines: list[tuple[float, bytes]]) -> None:
        self._lines = lines

    def __aiter__(self) -> TimedStdout:
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        delay, line = self._lines.pop(0)
        await asyncio.sleep(delay)
        return line


class BlockingStdout:
    def __init__(self, first_line: bytes | None = None) -> None:
        self.started = asyncio.Event()
        self._first_line = first_line

    def __aiter__(self) -> BlockingStdout:
        return self

    async def __anext__(self) -> bytes:
        if self._first_line is not None:
            line = self._first_line
            self._first_line = None
            return line
        self.started.set()
        await asyncio.Event().wait()
        raise StopAsyncIteration


class FakeStderr:
    def __init__(self, data: bytes = b"") -> None:
        self._data = data

    async def read(self) -> bytes:
        return self._data


class BlockingStderr:
    def __init__(self) -> None:
        self.cancelled = asyncio.Event()

    async def read(self) -> bytes:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return b""


class FakeProcess:
    def __init__(self, stdout_lines: list[bytes], returncode: int | None = 0) -> None:
        self.stdin = FakeStdin()
        self.stdout = FakeStdout(stdout_lines)
        self.stderr = FakeStderr()
        self.returncode = returncode
        self.pid = 123
        self.killed = False

    async def wait(self) -> int:
        return self.returncode or 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
