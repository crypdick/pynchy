"""Tests for SecurityGate -- session-scoped security enforcement."""

from __future__ import annotations

import pytest

from pynchy.host.container_manager.security.gate import (
    SecurityGate,
    create_gate,
    destroy_gate,
    get_gate,
    get_gate_for_group,
)
from pynchy.types import ServiceTrustConfig, WorkspaceSecurity


@pytest.fixture(autouse=True)
def _cleanup():
    """Ensure no gates leak between tests."""
    yield
    # Import the registry and clear it
    from pynchy.host.container_manager.security import gate as _mod

    _mod._gates.clear()


def _make_security(**services: ServiceTrustConfig) -> WorkspaceSecurity:
    return WorkspaceSecurity(services=dict(services))


class TestSecurityGateCreation:
    def test_create_and_get(self):
        security = _make_security()
        gate = create_gate("test-ws", 1000.0, security)
        assert isinstance(gate, SecurityGate)
        assert get_gate("test-ws", 1000.0) is gate

    def test_get_missing_returns_none(self):
        assert get_gate("nonexistent", 0.0) is None

    def test_destroy_removes_gate(self):
        security = _make_security()
        create_gate("test-ws", 1000.0, security)
        destroy_gate("test-ws", 1000.0)
        assert get_gate("test-ws", 1000.0) is None

    def test_destroy_missing_is_noop(self):
        destroy_gate("nonexistent", 0.0)  # Should not raise

    def test_concurrent_gates_different_timestamps(self):
        security = _make_security()
        gate1 = create_gate("test-ws", 1000.0, security)
        gate2 = create_gate("test-ws", 2000.0, security)
        assert gate1 is not gate2
        assert get_gate("test-ws", 1000.0) is gate1
        assert get_gate("test-ws", 2000.0) is gate2


class TestSecurityGateTaintPersistence:
    """Verify taint is sticky across calls (the bug fix)."""

    def test_corruption_taint_persists(self):
        security = _make_security(
            browser=ServiceTrustConfig(public_source=True, secret_data=False),
            slack=ServiceTrustConfig(public_source=False, public_sink=True),
        )
        gate = SecurityGate(security)

        # Reading from browser sets corruption taint
        result = gate.evaluate_read("browser")
        assert result.needs_cop
        assert gate.policy.corruption_tainted

        # Writing to slack should now need cop (because corruption tainted)
        result = gate.evaluate_write("slack", {})
        assert result.needs_cop

    def test_secret_taint_persists(self):
        security = _make_security(
            passwords=ServiceTrustConfig(public_source=False, secret_data=True),
            browser=ServiceTrustConfig(public_source=True),
        )
        gate = SecurityGate(security)

        gate.evaluate_read("passwords")
        assert gate.policy.secret_tainted

        # Taint persists for subsequent evaluations
        assert gate.policy.secret_tainted

    def test_taint_does_not_cross_gates(self):
        security = _make_security(
            browser=ServiceTrustConfig(public_source=True),
        )
        gate1 = create_gate("ws1", 1.0, security)
        gate2 = create_gate("ws2", 2.0, security)

        gate1.evaluate_read("browser")
        assert gate1.policy.corruption_tainted
        assert not gate2.policy.corruption_tainted


class TestGetGateForGroup:
    """Tests for get_gate_for_group — lookup by group folder only."""

    def test_returns_none_when_no_gates(self):
        assert get_gate_for_group("nonexistent") is None

    def test_returns_single_gate(self):
        security = _make_security()
        gate = create_gate("test-ws", 1000.0, security)
        assert get_gate_for_group("test-ws") is gate

    def test_returns_latest_timestamp(self):
        """When multiple gates exist for same group, returns the one with highest ts."""
        security = _make_security()
        _old = create_gate("test-ws", 1000.0, security)
        newest = create_gate("test-ws", 2000.0, security)
        assert get_gate_for_group("test-ws") is newest

    def test_does_not_return_other_groups(self):
        security = _make_security()
        create_gate("other-ws", 1000.0, security)
        assert get_gate_for_group("test-ws") is None

    def test_returns_correct_gate_among_multiple_groups(self):
        security = _make_security()
        create_gate("ws-a", 1000.0, security)
        gate_b = create_gate("ws-b", 2000.0, security)
        create_gate("ws-a", 3000.0, security)
        assert get_gate_for_group("ws-b") is gate_b


class TestSecurityGateEvaluate:
    def test_evaluate_read_delegates_to_policy(self):
        security = _make_security(
            browser=ServiceTrustConfig(public_source=True),
        )
        gate = SecurityGate(security)
        result = gate.evaluate_read("browser")
        assert result.allowed
        assert result.needs_cop

    def test_evaluate_write_delegates_to_policy(self):
        security = _make_security(
            slack=ServiceTrustConfig(public_sink=True, dangerous_writes=True),
        )
        gate = SecurityGate(security)
        result = gate.evaluate_write("slack", {})
        assert result.allowed
        assert result.needs_human

    def test_evaluate_read_forbidden(self):
        security = _make_security(
            blocked=ServiceTrustConfig(public_source="forbidden"),
        )
        gate = SecurityGate(security)
        result = gate.evaluate_read("blocked")
        assert not result.allowed


# ---------------------------------------------------------------------------
# Lifecycle tests (merged from test_gate_lifecycle.py)
# ---------------------------------------------------------------------------


class TestGateCreatedAtSpawn:
    def test_spawn_creates_gate(self):
        """Simulate what _spawn_container should do -- verify gate exists after."""
        gate = create_gate("test-ws", 12345.0, WorkspaceSecurity())
        assert gate is not None
        assert get_gate("test-ws", 12345.0) is gate

    def test_gate_accessible_by_group(self):
        """IPC handlers should find the gate by group folder."""
        create_gate("test-ws", 12345.0, WorkspaceSecurity())
        gate = get_gate_for_group("test-ws")
        assert gate is not None


class TestGateDestroyedOnRelease:
    def test_group_state_release_destroys_gate(self):
        """GroupState.release() should call destroy_gate."""
        from pynchy.host.orchestrator.concurrency import GroupState

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
        from pynchy.host.orchestrator.concurrency import GroupState

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
        from pynchy.host.orchestrator.concurrency import GroupQueue

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
        from pynchy.host.orchestrator.concurrency import GroupQueue

        queue = GroupQueue()
        queue.register_process("test@g.us", None, "pynchy-test", group_folder="test-ws")
        state = queue._get_group("test@g.us")
        assert state.invocation_ts == 0.0
