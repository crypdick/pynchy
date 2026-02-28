"""Tests for SecurityGate lifecycle -- creation at spawn, destruction at exit."""

from __future__ import annotations

import pytest

from pynchy.host.container_manager.security.gate import _gates, get_gate, get_gate_for_group


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    _gates.clear()


class TestGateCreatedAtSpawn:
    def test_spawn_creates_gate(self):
        """Simulate what _spawn_container should do -- verify gate exists after."""
        from pynchy.host.container_manager.security.gate import create_gate
        from pynchy.types import WorkspaceSecurity

        invocation_ts = 12345.0
        create_gate("test-ws", invocation_ts, WorkspaceSecurity())

        gate = get_gate("test-ws", invocation_ts)
        assert gate is not None

    def test_gate_accessible_by_group(self):
        """IPC handlers should find the gate by group folder."""
        from pynchy.host.container_manager.security.gate import create_gate
        from pynchy.types import WorkspaceSecurity

        create_gate("test-ws", 12345.0, WorkspaceSecurity())

        gate = get_gate_for_group("test-ws")
        assert gate is not None


class TestGateDestroyedOnRelease:
    def test_group_state_release_destroys_gate(self):
        """GroupState.release() should call destroy_gate."""
        from pynchy.group_queue import GroupState
        from pynchy.host.container_manager.security.gate import create_gate
        from pynchy.types import WorkspaceSecurity

        create_gate("test-ws", 100.0, WorkspaceSecurity())

        state = GroupState()
        state.group_folder = "test-ws"
        state.invocation_ts = 100.0
        state.active = True

        state.release()

        assert get_gate("test-ws", 100.0) is None
        assert state.invocation_ts == 0.0

    def test_release_without_gate_is_noop(self):
        """Release when no gate exists should not raise."""
        from pynchy.group_queue import GroupState

        state = GroupState()
        state.group_folder = "some-group"
        state.invocation_ts = 999.0
        state.active = True

        state.release()  # Should not raise


class TestInvocationTsOnContainerInput:
    def test_container_input_has_invocation_ts(self):
        """ContainerInput should have invocation_ts field with default 0.0."""
        from pynchy.types import ContainerInput

        ci = ContainerInput(
            messages=[],
            group_folder="test",
            chat_jid="test@g.us",
            is_admin=False,
        )
        assert ci.invocation_ts == 0.0

        ci.invocation_ts = 42.0
        assert ci.invocation_ts == 42.0


class TestRegisterProcessAcceptsInvocationTs:
    def test_register_process_stores_invocation_ts(self):
        """register_process() should accept and store invocation_ts."""
        from pynchy.group_queue import GroupQueue

        queue = GroupQueue()
        queue.register_process(
            "test@g.us",
            None,
            "pynchy-test",
            group_folder="test-ws",
            invocation_ts=42.0,
        )
        state = queue._get_group("test@g.us")
        assert state.invocation_ts == 42.0

    def test_register_process_defaults_invocation_ts_to_zero(self):
        """register_process() without invocation_ts should default to 0.0."""
        from pynchy.group_queue import GroupQueue

        queue = GroupQueue()
        queue.register_process("test@g.us", None, "pynchy-test", group_folder="test-ws")
        state = queue._get_group("test@g.us")
        assert state.invocation_ts == 0.0
