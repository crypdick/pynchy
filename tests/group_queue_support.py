"""Tests for the group queue."""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import make_settings

from pynchy.config.api import ContainerConfig, QueueConfig
from pynchy.host.orchestrator.api import ContainerRuntimeOperations
from pynchy.host.orchestrator.concurrency import GroupQueue, QueuePolicy
from pynchy.identifiers import RuntimeId
from pynchy.workspace.api import RuntimeTarget

TASK_EXPLODED_MESSAGE = "task exploded"


def _runtime(jid: str) -> RuntimeId:
    return RuntimeId(jid)


def _target(jid: str, folder: str | None = None) -> RuntimeTarget:
    return RuntimeTarget.from_binding(folder or jid, jid)


def _queue_policy(
    *,
    max_concurrent: int = 2,
    max_retries: int = 5,
    base_retry_seconds: float = 5.0,
) -> QueuePolicy:
    return QueuePolicy(
        max_concurrent=max_concurrent,
        max_retries=max_retries,
        retry_base_seconds=base_retry_seconds,
    )


@contextlib.contextmanager
def _patch_settings(
    *,
    max_concurrent: int = 2,
    base_retry_seconds: float = 5.0,
    data_dir=None,
):
    overrides = {
        "container": ContainerConfig(max_concurrent=max_concurrent),
        "queue": QueueConfig(base_retry_seconds=base_retry_seconds),
    }
    if data_dir is not None:
        overrides["data_dir"] = data_dir
    s = make_settings(**overrides)
    with patch("pynchy.host.container_manager.ipc.write._ipc_base_dir", s.data_dir / "ipc"):
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
    with _patch_settings(max_concurrent=2):
        runtime_queue = GroupQueue(_queue_policy(), container_runtime)
        yield runtime_queue
        await runtime_queue.shutdown()
