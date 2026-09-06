"""Queue state lease ownership boundary contracts."""

from __future__ import annotations

import pytest

from pynchy.host.orchestrator.queue_state import GroupState, HostProcessLease
from pynchy.identifiers import RuntimeId
from pynchy.workspace.api import RuntimeTarget


def test_stale_host_process_registration_is_ignored() -> None:
    target = RuntimeTarget.from_binding("group", "chat")
    state = GroupState(target)
    current = state.acquire_host_process(1)
    stale = HostProcessLease(RuntimeId("chat"), generation=2, owns_slot=current.owns_slot)

    assert state.register_host_process(stale, None, "container", 1.0) is False
    assert state.host_process_lease == current


def test_host_process_lease_cannot_be_acquired_twice() -> None:
    state = GroupState(RuntimeTarget.from_binding("group", "chat"))
    state.acquire_host_process(1)

    with pytest.raises(RuntimeError, match="A host process is already registered"):
        state.acquire_host_process(2)
