"""Composed plugin participants for session lifecycle transitions."""

from __future__ import annotations

import asyncio
import inspect

import pluggy  # noqa: TC002, RUF100 - beartype resolves plugin-manager annotations.

from pynchy.types import (
    WorkspaceProfile,  # noqa: TC001, RUF100 - beartype resolves lifecycle annotations.
)


async def prepare_context_reset(
    plugin_manager: pluggy.PluginManager | None,
    group: WorkspaceProfile,
) -> None:
    """Await every plugin concern before destructive session cleanup."""
    if plugin_manager is None:
        raise RuntimeError("Plugin manager is unavailable during context reset")
    hook = plugin_manager.hook.pynchy_before_context_reset
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
    results = await asyncio.gather(*contributions, return_exceptions=True)
    failures = [result for result in results if isinstance(result, BaseException)]
    if len(failures) == 1:
        raise failures[0]
    if failures:
        raise BaseExceptionGroup("Session lifecycle hooks failed", failures)
