"""Cycle-free control port for active Temporal workflows."""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

from temporalio.service import RPCError, RPCStatusCode

_TEMPORAL_RUNTIME_UNAVAILABLE = "Temporal scheduler runtime has not been started"


@runtime_checkable
class WorkflowCancellationHandle(Protocol):
    async def cancel(self) -> None: ...


@runtime_checkable
class WorkflowControlClient(Protocol):
    """Minimal Temporal client capability required for workflow cancellation."""

    def get_workflow_handle(
        self,
        workflow_id: str,
        *,
        run_id: str | None = None,
    ) -> WorkflowCancellationHandle: ...


_active_client: WorkflowControlClient | None = None


class TemporalRuntimeUnavailableError(RuntimeError):
    """Raised when workflow control runs before the Temporal client is active."""


def bind_workflow_client(client: WorkflowControlClient) -> None:
    """Expose the live client through the narrow workflow-control boundary."""
    global _active_client  # noqa: PLW0603 - one process owns one Temporal worker.
    _active_client = client


def unbind_workflow_client(client: WorkflowControlClient | None) -> None:
    """Clear only the client owned by the runtime that is stopping."""
    global _active_client  # noqa: PLW0603 - one process owns one Temporal worker.
    if _active_client is client:
        _active_client = None


async def _require_workflow_client() -> WorkflowControlClient:
    deadline = asyncio.get_running_loop().time() + 10.0
    while _active_client is None:
        if asyncio.get_running_loop().time() >= deadline:
            raise TemporalRuntimeUnavailableError(_TEMPORAL_RUNTIME_UNAVAILABLE)
        await asyncio.sleep(0.05)
    return _active_client


async def cancel_scheduled_agent_workflow(workflow_id: str) -> bool:
    """Cancel one active scheduled attempt by durable workflow identity."""
    client = await _require_workflow_client()
    try:
        await client.get_workflow_handle(workflow_id).cancel()
    except RPCError as exc:
        if exc.status is RPCStatusCode.NOT_FOUND:
            return False
        raise
    return True
