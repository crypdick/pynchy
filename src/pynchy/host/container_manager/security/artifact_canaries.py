"""Operational canary for credential-artifact taint propagation."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, cast

from pynchy.canary_contracts import CanaryExercise, CanaryRunContext
from pynchy.host.container_manager.api import (
    create_gate,
    destroy_gate,
    handle_artifact_security_check,
)
from pynchy.plugins.api import (  # noqa: TC001 - beartype resolves contract annotations at runtime.
    OutboundEvent,
)
from pynchy.workspace.api import (
    WorkspaceProfile,
    WorkspaceSecurity,
)

if TYPE_CHECKING:
    from pynchy.host.container_manager.ipc.handlers_artifact_security import (
        _ArtifactSecurityDeps,
    )


@dataclass(frozen=True)
class _FileTaintArtifact:
    secret_tainted: bool
    response_decision: str  # noqa: V107


class _SecurityCanaryDeps:
    def __init__(self, workspace: WorkspaceProfile) -> None:
        self._workspace = workspace

    async def broadcast_to_channels(self, _jid: str, _event: OutboundEvent) -> None: ...

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return {self._workspace.jid: self._workspace}


class FileSecretTaintCanary:
    """Prove credential-file access establishes sticky secret taint."""

    async def exercise(self, context: CanaryRunContext) -> CanaryExercise:
        source_group = f"security-canary-{context.run_id}"
        invocation_ts = monotonic()
        gate = create_gate(
            source_group,
            invocation_ts,
            WorkspaceSecurity(contains_secrets=False),
        )
        workspace = WorkspaceProfile(
            jid=f"security:{context.run_id}",
            name="Security canary",
            folder=source_group,
            trigger="always",
        )
        try:
            with tempfile.TemporaryDirectory(prefix="pynchy-security-canary-") as directory:
                response_path = Path(directory) / "response.json"
                await handle_artifact_security_check(
                    {
                        "request_id": context.run_id,
                        "tool_name": "Read",
                        "file_access": True,
                        "rule_ids": ["CRED001"],
                        "packages": [],
                    },
                    source_group,
                    is_admin=False,
                    deps=cast("_ArtifactSecurityDeps", _SecurityCanaryDeps(workspace)),
                    response_path_override=response_path,
                )
                response = json.loads(response_path.read_text(encoding="utf-8"))
            gate.notify_file_access()
            artifact = _FileTaintArtifact(
                secret_tainted=gate.secret_tainted,
                response_decision=str(response["result"]["decision"]),
            )
            return CanaryExercise(artifact=artifact)
        finally:
            destroy_gate(source_group, invocation_ts)

    async def verify(
        self,
        _context: CanaryRunContext,
        exercise: CanaryExercise,
    ) -> tuple[str, ...]:
        expected = _FileTaintArtifact(secret_tainted=True, response_decision="allow")
        if exercise.artifact != expected:
            raise RuntimeError("Artifact IPC did not establish sticky credential taint")
        return (
            "security:artifact-ipc:allow",
            "security:taint:credential:sticky",
        )

    async def cleanup(
        self,
        _context: CanaryRunContext,
        _exercise: CanaryExercise,
    ) -> tuple[str, ...]:
        return ()
