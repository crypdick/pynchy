"""Tests for SecurityGate -- session-scoped security enforcement."""

from __future__ import annotations

import pytest

from pynchy.security.gate import SecurityGate, create_gate, destroy_gate, get_gate
from pynchy.types import ServiceTrustConfig, WorkspaceSecurity


@pytest.fixture(autouse=True)
def _cleanup():
    """Ensure no gates leak between tests."""
    yield
    # Import the registry and clear it
    from pynchy.security import gate as _mod

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
