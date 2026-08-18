"""Backend-neutral, policy-mediated computer-use host action."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pluggy
from pydantic import ValidationError

from pynchy.actions.api import ActionId
from pynchy.plugins.api import (
    ApprovalContract,
    ApprovalMode,
    AuditContract,
    CapabilityDescriptor,
    CapabilityId,
    CapabilityKind,
    CapabilityProbeContext,
    CapabilityProbeResult,
    CapabilityRequirement,
    CapabilityRequirementKind,
    ComputerUseAction,
    ComputerUseBackend,
    ComputerUseConfig,
    ComputerUseRequest,
    HostActionAccess,
    HostActionDescriptor,
    HostActionHandler,
    HostActionRegistration,
    HostToolName,
    IdempotencyContract,
    IdempotencyMode,
    ProbeStatus,
    SourceGroup,
)

hookimpl = pluggy.HookimplMarker("pynchy")

_ACTION_IDS = tuple(
    ActionId(action_id)
    for action_id in (
        "desktop.computer.capture",
        "desktop.computer.app.list",
        "desktop.computer.window.list",
        "desktop.computer.app.launch",
        "desktop.computer.click",
        "desktop.computer.double.click",
        "desktop.computer.right.click",
        "desktop.computer.text.type",
        "desktop.computer.key.send",
        "desktop.computer.scroll",
        "desktop.computer.element.value.set",
        "desktop.computer.element.action.perform",
        "desktop.computer.menu.list",
        "desktop.computer.menu.click",
        "desktop.computer.dialog.list",
        "desktop.computer.dialog.click",
        "desktop.computer.dialog.input",
        "desktop.computer.dialog.file",
        "desktop.computer.dialog.dismiss",
        "desktop.computer.clipboard.get",
        "desktop.computer.clipboard.set",
        "desktop.computer.clipboard.clear",
        "desktop.computer.clipboard.save",
        "desktop.computer.clipboard.restore",
        "desktop.computer.space.list",
        "desktop.computer.space.switch",
        "desktop.computer.space.window.move",
        "desktop.computer.wait",
        "desktop.computer.permissions.check",
    )
)


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _slug(value: object) -> str:
    if not isinstance(value, str):
        return "computer-use"
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48] or "computer-use"


def _artifact_path(*, data_dir: Path | None, source_group: SourceGroup, label: object) -> Path:
    if data_dir is None:
        raise RuntimeError("computer-use capture requires lifecycle configuration")
    filename = f"{_timestamp()}-{_slug(label)}.png"
    return data_dir / "ipc" / source_group / "computer-use" / filename


def _backend_catalog(
    candidates: tuple[ComputerUseBackend, ...],
) -> dict[str, ComputerUseBackend]:
    catalog: dict[str, ComputerUseBackend] = {}
    for candidate in candidates:
        if not isinstance(candidate, ComputerUseBackend):
            raise TypeError("pynchy_computer_use_backend returned an invalid provider")
        if candidate.name in catalog:
            raise ValueError(f"duplicate computer-use provider: {candidate.name}")
        catalog[candidate.name] = candidate
    return catalog


def _configured_backend(
    config: ComputerUseConfig | None,
    backends: dict[str, ComputerUseBackend],
) -> ComputerUseBackend | None:
    if config is None:
        return None
    backend = backends.get(config.provider)
    if backend is not None:
        return backend
    available = ", ".join(sorted(backends)) or "none"
    raise ValueError(
        f"configured computer-use provider {config.provider!r} is not loaded; "
        f"available providers: {available}"
    )


async def _probe_backend(
    _context: CapabilityProbeContext,
    backend: ComputerUseBackend | None,
) -> CapabilityProbeResult:
    if backend is None:
        return CapabilityProbeResult(
            ProbeStatus.UNAVAILABLE,
            "computer-use provider is not configured",
        )
    status = await asyncio.to_thread(backend.availability)
    if status.available:
        return CapabilityProbeResult(ProbeStatus.READY)
    return CapabilityProbeResult(
        ProbeStatus.UNAVAILABLE,
        f"computer-use provider {backend.name} is unavailable: {status.reason or 'unavailable'}",
    )


def _capability(
    backend: ComputerUseBackend | None,
) -> CapabilityDescriptor:
    async def probe(context: CapabilityProbeContext) -> CapabilityProbeResult:
        return await _probe_backend(context, backend)

    return CapabilityDescriptor(
        id=CapabilityId("desktop.computer.use"),
        kind=CapabilityKind.HOST_ACTION,
        owner="computer-use",
        summary="Inspect and operate a desktop through a policy-mediated provider plugin.",
        action_ids=_ACTION_IDS,
        requirements=(
            CapabilityRequirement(
                kind=CapabilityRequirementKind.WORKSPACE_TOOL,
                name="computer_use",
                description="Enable the computer_use tool for this workspace.",
            ),
            CapabilityRequirement(
                kind=CapabilityRequirementKind.HOST_BINARY,
                name="computer-use-provider",
                description=(
                    f"Load configured provider plugin {backend.name}."
                    if backend is not None
                    else "Configure one computer-use provider plugin."
                ),
            ),
        ),
        setup_hint="Enable a computer-use provider plugin supported by this host platform.",
        recovery_hint="Check the selected provider's installation and platform permissions.",
        documentation="docs/usage/host-capabilities/computer-use.md",
        probe=probe,
    )


async def _execute_request(
    request: ComputerUseRequest,
    data_dir: Path | None,
    backend: ComputerUseBackend | None,
) -> dict[str, Any]:
    if request.action is ComputerUseAction.WAIT:
        await asyncio.sleep(request.seconds)
        return {
            "result": {
                "action": request.action.value,
                "backend": "host",
                "output": f"waited {request.seconds:g}s",
            }
        }
    if backend is None:
        raise RuntimeError("computer-use provider is not configured")
    screenshot_path = None
    if request.action is ComputerUseAction.CAPTURE:
        screenshot_path = _artifact_path(
            data_dir=data_dir,
            source_group=request.source_group,
            label=request.label,
        )
    result = await backend.execute(request, screenshot_path=screenshot_path)
    result["action"] = request.action.value
    if request.capture_after and request.action is not ComputerUseAction.CAPTURE:
        capture = request.model_copy(
            update={
                "action": ComputerUseAction.CAPTURE,
                "capture_after": False,
                "label": f"after-{request.action.value}",
            }
        )
        after_path = _artifact_path(
            data_dir=data_dir,
            source_group=request.source_group,
            label=capture.label,
        )
        result["after"] = await backend.execute(capture, screenshot_path=after_path)
    return {"result": result}


def _handler(
    data_dir: Path | None,
    backend: ComputerUseBackend | None,
) -> HostActionHandler:
    async def handle(data: dict[str, Any]) -> dict[str, Any]:
        try:
            request = ComputerUseRequest.parse(data)
            return await _execute_request(request, data_dir, backend)
        except (RuntimeError, TypeError, ValidationError, ValueError) as exc:
            return {"error": str(exc)}

    return handle


class ComputerUsePlugin:
    """Expose one selected computer-use provider through a neutral host action."""

    def __init__(
        self,
        config: ComputerUseConfig | None = None,
        *,
        data_dir: Path | None = None,
    ) -> None:
        self._config = config
        self._data_dir = data_dir

    def configure(self, config: ComputerUseConfig | None, *, data_dir: Path) -> None:
        """Apply provider selection and lifecycle paths before registration."""
        self._config = config
        self._data_dir = data_dir

    @hookimpl
    def pynchy_service_handler(
        self,
        computer_use_backends: tuple[ComputerUseBackend, ...],
    ) -> HostActionRegistration:
        backends = _backend_catalog(computer_use_backends)
        backend = _configured_backend(self._config, backends)
        descriptor = HostActionDescriptor(
            capability=_capability(backend),
            tool_name=HostToolName("computer_use"),
            handler=_handler(self._data_dir, backend),
            access=HostActionAccess.WRITE,
            approval=ApprovalContract(mode=ApprovalMode.SESSION_TOOL),
            idempotency=IdempotencyContract(IdempotencyMode.IPC_REQUEST_ID),
            audit=AuditContract(),
        )
        return HostActionRegistration(actions=(descriptor,))

    @hookimpl
    def pynchy_skill_paths(self) -> list[str]:
        skill_dir = Path(__file__).resolve().parent / "skills" / "computer-use"
        if skill_dir.is_dir():
            return [str(skill_dir)]
        return []
