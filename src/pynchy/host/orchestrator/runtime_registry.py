"""Stable runtime identity and current control-address bindings."""

from __future__ import annotations

from collections.abc import (
    ValuesView,
)

from pynchy.host.orchestrator.queue_state import GroupState
from pynchy.identifiers import (
    RuntimeId,
)
from pynchy.workspace.api import (
    RuntimeTarget,
)


class RuntimeRegistry:
    """Own transient state for each stable execution runtime."""

    def __init__(self) -> None:
        self._states: dict[RuntimeId, GroupState] = {}

    @property
    def states(self) -> dict[RuntimeId, GroupState]:
        """Expose the owned mapping to queue shutdown orchestration."""
        return self._states

    def bind(self, target: RuntimeTarget) -> GroupState:
        """Bind an idle runtime to its current control address."""
        state = self._states.get(target.id)
        if state is None:
            state = GroupState(target=target)
            self._states[target.id] = state
            return state
        if state.target == target:
            return state
        if self.has_activity(state):
            raise RuntimeError(f"Cannot rebind active runtime {target.id!r}")
        state.target = target
        return state

    def require(self, runtime_id: RuntimeId) -> GroupState:
        """Return state for a runtime that has already entered the queue."""
        try:
            return self._states[runtime_id]
        except KeyError as exc:
            raise RuntimeError(f"Runtime has not entered the queue: {runtime_id}") from exc

    def get(self, runtime_id: RuntimeId) -> GroupState | None:
        return self._states.get(runtime_id)

    def values(self) -> ValuesView[GroupState]:
        return self._states.values()

    @staticmethod
    def has_activity(state: GroupState) -> bool:
        return bool(state.active or state.pending_tasks or state.host_process_lease is not None)
