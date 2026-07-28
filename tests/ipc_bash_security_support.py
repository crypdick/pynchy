"""Tests for the bash security check IPC handler."""

from __future__ import annotations

from conftest import NullIpcDeps

from pynchy.host.container_manager.security.cop import (
    CopCommandDecision,
    CopCommandVerdict,
)
from pynchy.host.container_manager.security.gate import SecurityGate
from pynchy.workspace.api import (
    ServiceTrustConfig,
    WorkspaceProfile,
    WorkspaceSecurity,
)


def _make_gate(
    *,
    corruption: bool = False,
    secret: bool = False,
    cop_active: bool = True,
) -> SecurityGate:
    services = {}
    if corruption:
        services["browser"] = ServiceTrustConfig(
            public_source=True,
            secret_data=False,
            public_sink=False,
            dangerous_writes=False,
        )
    if secret:
        services["passwords"] = ServiceTrustConfig(
            public_source=False,
            secret_data=True,
            public_sink=False,
            dangerous_writes=False,
        )
    gate = SecurityGate(
        WorkspaceSecurity(
            services=services,
            cop_active=cop_active,
        )
    )
    if corruption:
        gate.evaluate_read("browser")
    if secret:
        gate.evaluate_read("passwords")
    return gate


class _Deps(NullIpcDeps):
    def __init__(self, workspace: WorkspaceProfile) -> None:
        self._workspace = workspace
        self.events: list[object] = []

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return {self._workspace.jid: self._workspace}

    async def broadcast_to_channels(self, _jid: str, event: object) -> None:
        self.events.append(event)


def _cop_verdict(
    decision: CopCommandDecision,
    reason: str,
    *,
    degraded: bool = False,
) -> CopCommandVerdict:
    return CopCommandVerdict(decision=decision, reason=reason, degraded=degraded)
