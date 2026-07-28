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
from pynchy.workspace.api import (
    WorkspaceProfile,
    WorkspaceSecurity,
)

if TYPE_CHECKING:
    from pynchy.host.container_manager.ipc import IpcDeps
    from pynchy.plugins.api import (
        Channel,
        OutboundEvent,
    )


@dataclass(frozen=True)
class _FileTaintArtifact:
    secret_tainted: bool
    response_decision: str


class _SecurityCanaryDeps:
    def __init__(self, workspace: WorkspaceProfile) -> None:
        self._workspace = workspace

    async def broadcast_to_channels(self, _jid: str, _event: OutboundEvent) -> None: ...

    async def broadcast_host_message(self, _jid: str, _text: str) -> None: ...

    async def broadcast_system_notice(self, _jid: str, _text: str) -> None: ...

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return {self._workspace.jid: self._workspace}

    def register_workspace(self, _profile: WorkspaceProfile) -> None: ...

    async def sync_group_metadata(self, *, force: bool) -> None:
        del force

    async def get_available_groups(self) -> list[object]:
        return []

    def write_groups_snapshot(
        self,
        _group_folder: str,
        _available_groups: list[object],
        _registered_jids: set[str],
        *,
        is_admin: bool,
    ) -> None:
        del is_admin

    def has_active_session(self, _group_folder: str) -> bool:
        return False

    async def clear_session(self, _group_folder: str) -> None: ...

    def get_active_sessions(self) -> dict[str, str]:
        return {}

    async def clear_chat_history(self, _chat_jid: str) -> None: ...

    def enqueue_message_check(self, _group_jid: str) -> None: ...

    def channels(self) -> list[Channel]:
        return []

    async def request_deploy(
        self,
        *,
        chat_jid: str | None,
        commit_sha: str,
        rebuild: bool,
        resume_prompt: str,
    ) -> None:
        del chat_jid, commit_sha, rebuild, resume_prompt

    async def trigger_deploy(self, _previous_sha: str, *, rebuild: bool = True) -> None:
        del rebuild

    async def create_periodic_agent(self, _request: object) -> None: ...

    async def get_scheduled_work_status(
        self,
        *,
        source_group: str,
        is_admin: bool,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        del source_group, is_admin
        return [], []


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
                    deps=cast("IpcDeps", _SecurityCanaryDeps(workspace)),
                    response_path_override=response_path,
                )
                response = json.loads(response_path.read_text(encoding="utf-8"))
            gate.notify_file_access()
            artifact = _FileTaintArtifact(
                secret_tainted=gate.policy.secret_tainted,
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
