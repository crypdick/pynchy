"""Tests for the group queue."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import make_settings

from pynchy.host.orchestrator.api import ContainerRuntimeOperations
from pynchy.host.orchestrator.concurrency import GroupQueue
from pynchy.identifiers import RuntimeId
from pynchy.workspace.api import RuntimeTarget

TASK_EXPLODED_MESSAGE = "task exploded"


async def start_queued(coro):
    """Start an awaiting queue owner and let it reach admission."""
    owner = asyncio.create_task(coro)
    await asyncio.sleep(0)
    return owner


def _runtime(jid: str) -> RuntimeId:
    return RuntimeId(jid)


def _target(jid: str, folder: str | None = None) -> RuntimeTarget:
    return RuntimeTarget.from_binding(folder or jid, jid)


@contextlib.contextmanager
def _patch_settings(*, data_dir=None):
    settings = make_settings(**({"data_dir": data_dir} if data_dir is not None else {}))
    with patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"):
        yield


@pytest.fixture
def container_runtime() -> ContainerRuntimeOperations:
    return ContainerRuntimeOperations(
        write_message=MagicMock(),
        write_close_sentinel=MagicMock(),
        clean_input_dir=MagicMock(),
        destroy_gate=MagicMock(),
        destroy_session=AsyncMock(),
        destroy_all_sessions=AsyncMock(),
        graceful_stop=AsyncMock(),
    )


@pytest.fixture
async def queue(container_runtime: ContainerRuntimeOperations):
    with _patch_settings():
        runtime_queue = GroupQueue(2, container_runtime)
        yield runtime_queue
        await runtime_queue.shutdown()
