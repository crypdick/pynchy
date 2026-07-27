"""Backend-neutral, policy-mediated computer-use host action."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pluggy
from pydantic import ValidationError

from pynchy.actions import ActionId
from pynchy.capabilities import (
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
    HostActionAccess,
    HostActionDescriptor,
    HostActionHandler,
    HostActionRegistration,
    HostToolName,
    IdempotencyContract,
    IdempotencyMode,
    ProbeStatus,
)
from pynchy.config import get_settings
from pynchy.plugins.computer_use import (
    ComputerUseAction,
    ComputerUseBackend,
    ComputerUseRequest,
    ComputerUseRouterConfig,
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


def _artifact_path(*, source_group: SourceGroup, label: object) -> Path:
    filename = f"{_timestamp()}-{_slug(label)}.png"
    return get_settings().data_dir / "ipc" / source_group / "computer-use" / filename


def _plugin_config() -> ComputerUseRouterConfig:
    plugin = get_settings().plugins.get("computer-use")
    options = plugin.options if plugin is not None else {}
    return ComputerUseRouterConfig.model_validate(options)


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


async def _select_backend(
    config: ComputerUseRouterConfig,
    backends: dict[str, ComputerUseBackend],
) -> tuple[ComputerUseBackend, int, tuple[str, ...]]:
    configured = [(name, backends.get(name)) for name in config.providers]
    available = [(name, backend) for name, backend in configured if backend is not None]
    statuses = await asyncio.gather(
        *(asyncio.to_thread(backend.availability) for _, backend in available)
    )
    by_name = {name: status for (name, _), status in zip(available, statuses, strict=True)}
    reasons: list[str] = []
    for index, (name, backend) in enumerate(configured):
        if backend is None:
            reasons.append(f"{name}: plugin is not loaded")
            continue
        status = by_name[name]
        if status.available:
            return backend, index, tuple(reasons)
        reasons.append(f"{name}: {status.reason or 'unavailable'}")
    detail = "; ".join(reasons) or "no providers configured"
    raise RuntimeError(f"No configured computer-use provider is available: {detail}")


async def _probe_backend(
    _context: CapabilityProbeContext,
    config: ComputerUseRouterConfig,
    backends: dict[str, ComputerUseBackend],
) -> CapabilityProbeResult:
    try:
        backend, index, reasons = await _select_backend(config, backends)
    except RuntimeError as exc:
        return CapabilityProbeResult(ProbeStatus.UNAVAILABLE, str(exc))
    if index:
        return CapabilityProbeResult(
            ProbeStatus.DEGRADED,
            f"Using fallback provider {backend.name}: {'; '.join(reasons)}",
        )
    return CapabilityProbeResult(ProbeStatus.READY)


def _capability(
    config: ComputerUseRouterConfig,
    backends: dict[str, ComputerUseBackend],
) -> CapabilityDescriptor:
    async def probe(context: CapabilityProbeContext) -> CapabilityProbeResult:
        return await _probe_backend(context, config, backends)

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
                    "Load and configure one of these provider plugins: "
                    f"{', '.join(config.providers) or '(none)'}"
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
    config: ComputerUseRouterConfig,
    backends: dict[str, ComputerUseBackend],
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
    backend, _, _ = await _select_backend(config, backends)
    screenshot_path = None
    if request.action is ComputerUseAction.CAPTURE:
        screenshot_path = _artifact_path(source_group=request.source_group, label=request.label)
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
        after_path = _artifact_path(source_group=request.source_group, label=capture.label)
        result["after"] = await backend.execute(capture, screenshot_path=after_path)
    return {"result": result}


def _handler(
    config: ComputerUseRouterConfig,
    backends: dict[str, ComputerUseBackend],
) -> HostActionHandler:
    async def handle(data: dict[str, Any]) -> dict[str, Any]:
        try:
            request = ComputerUseRequest.parse(data)
            return await _execute_request(request, config, backends)
        except (RuntimeError, TypeError, ValidationError, ValueError) as exc:
            return {"error": str(exc)}

    return handle


class ComputerUsePlugin:
    """Route one neutral host action through optional platform provider plugins."""

    def __init__(self, config: ComputerUseRouterConfig | None = None) -> None:
        self._config = config

    @hookimpl
    def pynchy_service_handler(
        self,
        computer_use_backends: tuple[ComputerUseBackend, ...],
    ) -> HostActionRegistration:
        config = self._config or _plugin_config()
        backends = _backend_catalog(computer_use_backends)
        descriptor = HostActionDescriptor(
            capability=_capability(config, backends),
            tool_name=HostToolName("computer_use"),
            handler=_handler(config, backends),
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
