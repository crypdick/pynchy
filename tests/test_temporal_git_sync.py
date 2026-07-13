"""Tests for the Temporal host-repository synchronization activity."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pynchy.host.orchestrator.temporal import git_sync

if TYPE_CHECKING:
    import pytest


async def test_host_git_sync_skips_the_hermetic_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generated runtime must not deploy-restart its unsupervised process."""
    monkeypatch.setenv("PYNCHY_RUNTIME_HARNESS", "1")
    called = False

    def require_scheduler_deps() -> object:
        nonlocal called
        called = True
        return object()

    monkeypatch.setattr(git_sync, "_require_scheduler_deps", require_scheduler_deps)
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        git_sync,
        "_record_activity_result",
        lambda task_id, result: recorded.append((task_id, result)),
    )

    assert await git_sync.run_host_git_sync() == "skipped"
    assert not called
    assert recorded == [(git_sync.HOST_GIT_SYNC_ID, "skipped")]
