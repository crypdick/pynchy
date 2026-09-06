"""Composed plugin participants for session lifecycle transitions."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable
from typing import Protocol, cast, runtime_checkable

from pynchy.workspace.api import (
    WorkspaceProfile,
)


@runtime_checkable
class _LifecycleHook(Protocol):
    def get_hookimpls(self) -> list[object]: ...

    def __call__(self, *, group: WorkspaceProfile) -> list[object]: ...


@runtime_checkable
class _PluginHooks(Protocol):
    pynchy_before_context_reset: _LifecycleHook


@runtime_checkable  # noqa: V102
class _PluginManager(Protocol):
    hook: _PluginHooks


async def prepare_context_reset(
    plugin_manager: object | None,
    group: WorkspaceProfile,
) -> None:
    """Await every plugin concern before destructive session cleanup."""
    if plugin_manager is None:
        raise RuntimeError("Plugin manager is unavailable during context reset")
    hook = cast("_PluginManager", plugin_manager).hook.pynchy_before_context_reset
    implementations = hook.get_hookimpls()
    contributions = hook(group=group)
    if len(contributions) != len(implementations):
        for contribution in contributions:
            if inspect.iscoroutine(contribution):
                contribution.close()
        raise TypeError("Session lifecycle hooks must return an awaitable")
    for contribution in contributions:
        if not inspect.isawaitable(contribution):
            for pending in contributions:
                if inspect.iscoroutine(pending):
                    pending.close()
            raise TypeError("Session lifecycle hooks must return an awaitable")
    results = await asyncio.gather(
        *cast("list[Awaitable[object]]", contributions), return_exceptions=True
    )
    failures = [result for result in results if isinstance(result, BaseException)]
    if len(failures) == 1:
        raise failures[0]
    if failures:
        raise BaseExceptionGroup("Session lifecycle hooks failed", failures)
