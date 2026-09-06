"""Tests for SecurityGate worker-scoped security enforcement."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from conftest import make_container_runtime_operations

from pynchy.agent_protocol.api import ContainerInput
from pynchy.config.api import validate_settings_mapping
from pynchy.conversation.api import dynamic_thread_folder
from pynchy.host.container_manager.security.gate import (
    SecurityGate,
    create_gate,
    destroy_gate,
    get_gate,
    get_gate_for_group,
    resolve_security,
)
from pynchy.host.container_manager.session import ContainerSession
from pynchy.host.orchestrator.concurrency import GroupQueue
from pynchy.host.orchestrator.queue_state import GroupState
from pynchy.workspace.api import (
    RuntimeTarget,
    ServiceTrustConfig,
    WorkspaceSecurity,
)


@pytest.fixture(autouse=True)
def _cleanup(monkeypatch: pytest.MonkeyPatch):
    """Ensure gates made through the public registry API do not leak between tests."""
    created: list[tuple[str, float]] = []
    original_create_gate = create_gate

    def track_created_gate(
        source_group: str,
        invocation_ts: float,
        security: WorkspaceSecurity,
        *,
        public_source_input: bool = False,
        secret_source_input: bool = False,
    ) -> SecurityGate:
        created.append((source_group, invocation_ts))
        return original_create_gate(
            source_group,
            invocation_ts,
            security,
            public_source_input=public_source_input,
            secret_source_input=secret_source_input,
        )

    monkeypatch.setitem(globals(), "create_gate", track_created_gate)
    yield
    for source_group, invocation_ts in created:
        destroy_gate(source_group, invocation_ts)


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

    def test_public_source_input_starts_corruption_tainted(self):
        gate = create_gate(
            "test-ws",
            1000.0,
            _make_security(
                slack=ServiceTrustConfig(
                    public_source=False,
                    secret_data=False,
                    public_sink=True,
                    dangerous_writes=False,
                )
            ),
            public_source_input=True,
        )

        assert gate.corruption_tainted
        assert gate.evaluate_write("slack", {}).needs_cop

    def test_private_external_input_gates_a_public_sink(self):
        gate = create_gate(
            "matrix-route",
            1001.0,
            _make_security(
                slack=ServiceTrustConfig(
                    public_source=False,
                    secret_data=False,
                    public_sink=True,
                    dangerous_writes=False,
                )
            ),
            public_source_input=True,
            secret_source_input=True,
        )

        decision = gate.evaluate_write("slack", {})
        assert gate.corruption_tainted is True
        assert gate.secret_tainted is True
        assert decision.needs_cop is True
        assert decision.needs_human is True


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
        assert gate.corruption_tainted

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
        assert gate.secret_tainted

        # Taint persists for subsequent evaluations
        assert gate.secret_tainted

    def test_taint_does_not_cross_gates(self):
        security = _make_security(
            browser=ServiceTrustConfig(public_source=True),
        )
        gate1 = create_gate("ws1", 1.0, security)
        gate2 = create_gate("ws2", 2.0, security)

        gate1.evaluate_read("browser")
        assert gate1.corruption_tainted
        assert not gate2.corruption_tainted

    def test_admin_resolved_trust_keeps_forbidden_writes(self, monkeypatch):
        monkeypatch.setenv("LINEAR_API_KEY", "configured")
        settings = validate_settings_mapping(
            {
                "profiles": {
                    "admin": {
                        "is_admin": True,
                        "tools": ["linear"],
                    }
                },
                "workspaces": {"admin": {"profiles": ["admin"]}},
                "tools": {
                    "linear": {
                        "type": "linear",
                        "public_source": False,
                        "secret_data": False,
                        "public_sink": False,
                        "dangerous_writes": "forbidden",
                    }
                },
            }
        )
        monkeypatch.setattr("pynchy.config.api.get_settings", lambda: settings)

        gate = SecurityGate(resolve_security("admin", is_admin=True))

        decision = gate.evaluate_write("linear", {})
        assert decision.allowed is False

    def test_named_linear_account_owns_host_action_policy(self, monkeypatch):
        monkeypatch.setenv("LINEAR_API_KEY", "configured")
        settings = validate_settings_mapping(
            {
                "profiles": {"synapse": {"tools": ["linear_synapse"]}},
                "workspaces": {"synapse": {"profiles": ["synapse"]}},
                "tools": {
                    "linear_synapse": {
                        "type": "linear",
                        "public_source": False,
                        "secret_data": True,
                        "public_sink": "forbidden",
                        "dangerous_writes": False,
                    }
                },
            }
        )
        monkeypatch.setattr("pynchy.config.api.get_settings", lambda: settings)

        gate = SecurityGate(resolve_security("synapse", is_admin=False))

        assert gate.evaluate_read("linear").allowed is True
        assert gate.evaluate_write("linear", {}).allowed is False


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


class TestResolveSecurity:
    def test_resolves_selected_tool_trust(self, monkeypatch):
        settings = validate_settings_mapping(
            {
                "profiles": {"worker": {"tools": ["safe-tool"]}},
                "workspaces": {"research": {"profiles": ["worker"]}},
                "tools": {
                    "safe-tool": {
                        "type": "builtin",
                        "name": "safe-tool",
                        "public_source": False,
                        "secret_data": False,
                        "public_sink": False,
                        "dangerous_writes": False,
                    }
                },
            }
        )
        monkeypatch.setattr("pynchy.config.api.get_settings", lambda: settings)

        security = resolve_security("research")

        assert security.services == {
            "safe-tool": ServiceTrustConfig(
                public_source=False,
                secret_data=False,
                public_sink=False,
                dangerous_writes=False,
            )
        }
        assert security.capabilities == {}

    def test_preserves_contains_secrets_from_resolved_profiles(self, monkeypatch):
        settings = validate_settings_mapping(
            {
                "profiles": {"worker": {"contains_secrets": True}},
                "workspaces": {"research": {"profiles": ["worker"]}},
            }
        )
        monkeypatch.setattr("pynchy.config.api.get_settings", lambda: settings)

        security = resolve_security("research")

        assert security.contains_secrets is True
        assert security.services == {}

    def test_dynamic_thread_uses_parent_workspace_security(self, monkeypatch):
        monkeypatch.setenv("LINEAR_API_KEY", "configured")
        settings = validate_settings_mapping(
            {
                "profiles": {
                    "worker": {
                        "contains_secrets": True,
                        "tools": ["linear"],
                    }
                },
                "workspaces": {"research": {"profiles": ["worker"]}},
                "tools": {
                    "linear": {
                        "type": "linear",
                        "public_source": False,
                        "secret_data": False,
                        "public_sink": False,
                        "dangerous_writes": False,
                    }
                },
            }
        )
        monkeypatch.setattr("pynchy.config.api.get_settings", lambda: settings)

        security = resolve_security(dynamic_thread_folder("research", "thread:123"))

        assert security.contains_secrets is True
        assert security.services == {
            "linear": ServiceTrustConfig(
                public_source=False,
                secret_data=False,
                public_sink=False,
                dangerous_writes=False,
            )
        }

    def test_dynamic_thread_uses_parent_workspace_capability_rules(self, monkeypatch):
        settings = validate_settings_mapping(
            {
                "profiles": {
                    "worker": {
                        "tools": ["email"],
                        "permissions": {
                            "deny": ["mcp.email.send"],
                            "allow": ["mcp.email.preview"],
                        },
                    }
                },
                "workspaces": {"research": {"profiles": ["worker"]}},
                "tools": {
                    "email": {
                        "type": "builtin",
                        "public_source": False,
                        "secret_data": False,
                        "public_sink": False,
                        "dangerous_writes": False,
                    }
                },
            }
        )
        monkeypatch.setattr("pynchy.config.api.get_settings", lambda: settings)

        security = resolve_security(dynamic_thread_folder("research", "thread:123"))

        assert security.capabilities["mcp.email.send"].decision == "deny"
        assert security.capabilities["mcp.email.preview"].decision == "allow"

    def test_admin_resolution_preserves_contains_secrets(self, monkeypatch):
        settings = validate_settings_mapping(
            {
                "profiles": {
                    "admin": {"is_admin": True, "contains_secrets": True},
                },
                "workspaces": {"admin": {"profiles": ["admin"]}},
            }
        )
        monkeypatch.setattr("pynchy.config.api.get_settings", lambda: settings)

        security = resolve_security("admin", is_admin=True)

        assert security.contains_secrets is True
        assert security.services == {}


class TestGateLifecycle:
    def test_container_queue_release_retains_durable_worker_gate(self):
        """A warm container turn must retain the worker's security state."""
        create_gate("test-ws", 100.0, WorkspaceSecurity())

        state = GroupState(target=RuntimeTarget.from_binding("test-ws", "test@g.us"))
        state.invocation_ts = 100.0
        state.active = True

        state.release()

        assert get_gate("test-ws", 100.0) is not None
        assert state.invocation_ts == 0
        destroy_gate("test-ws", 100.0)

    def test_release_without_gate_is_noop(self):
        """Release when no gate exists should not raise."""
        state = GroupState(target=RuntimeTarget.from_binding("some-group", "some@g.us"))
        state.invocation_ts = 999.0
        state.active = True

        state.release()  # Should not raise

    async def test_container_session_stop_retains_gate_until_worker_stops(self):
        """Stopping the worker retains its gate while IPC requests can still drain."""
        create_gate("test-ws", 100.0, WorkspaceSecurity())
        session = ContainerSession(
            "test-ws",
            "pynchy-test-ws",
            invocation_ts=100.0,
        )
        session.proc = AsyncMock()
        session.proc.returncode = None

        def assert_gate_exists_while_stopping(*_args: object) -> None:
            assert get_gate("test-ws", 100.0) is not None

        with (
            patch(
                "pynchy.host.container_manager.session.graceful_stop",
                side_effect=assert_gate_exists_while_stopping,
            ),
            patch(
                "pynchy.host.container_manager.session.docker_rm_force",
                new_callable=AsyncMock,
            ),
        ):
            await session.stop()

        assert get_gate("test-ws", 100.0) is None


class TestInvocationTsOnContainerInput:
    def test_container_input_has_invocation_ts(self):
        """ContainerInput should have invocation_ts field with default 0.0."""
        ci = ContainerInput(
            messages=[],
            group_folder="test",
            chat_jid="test@g.us",
            is_admin=False,
        )
        assert ci.invocation_ts == 0

        ci.invocation_ts = 42.0
        assert ci.invocation_ts == 42


class TestRegisterHostProcessInvocation:
    def test_release_destroys_matching_invocation_gate(self):
        """A registered host process releases the gate for its invocation."""
        queue = GroupQueue(
            10,
            replace(make_container_runtime_operations(), destroy_gate=destroy_gate),
        )
        gate = create_gate("test-ws", 42.0, WorkspaceSecurity())
        lease = queue.acquire_host_process(RuntimeTarget.from_binding("test-ws", "test@g.us"))

        assert (
            queue.register_host_process(
                lease,
                None,
                "pynchy-test",
                invocation_ts=42.0,
            )
            is True
        )

        assert queue.release_host_process(lease) is False
        assert get_gate("test-ws", 42.0) is None
        assert gate is not None
