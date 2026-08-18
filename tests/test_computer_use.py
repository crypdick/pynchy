"""Tests for the backend-neutral computer-use service."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from pynchy.plugins import get_plugin_manager
from pynchy.plugins.api import (
    ApprovalMode,
    CapabilityProbeContext,
    ComputerUseBackendAvailability,
    ComputerUseConfig,
    ComputerUseRequest,
    HostActionHandler,
    ProbeStatus,
    get_host_action_catalog,
)
from pynchy.plugins.integrations.computer_use import ComputerUsePlugin


@dataclass
class _RecordingBackend:
    backend_name: str
    is_available: bool = True
    reason: str | None = None
    requests: list[ComputerUseRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.backend_name

    def availability(self) -> ComputerUseBackendAvailability:
        return ComputerUseBackendAvailability(
            available=self.is_available,
            reason=self.reason,
        )

    async def execute(
        self,
        request: ComputerUseRequest,
        *,
        screenshot_path: Path | None = None,
    ) -> dict[str, Any]:
        self.requests.append(request)
        return {
            "backend": self.name,
            "output": {"screenshot_path": str(screenshot_path) if screenshot_path else None},
        }


@dataclass
class _FailingBackend(_RecordingBackend):
    async def execute(
        self,
        request: ComputerUseRequest,
        *,
        screenshot_path: Path | None = None,
    ) -> dict[str, Any]:
        self.requests.append(request)
        raise RuntimeError("provider failed after starting the action")


def _handler(
    *backends: _RecordingBackend,
    config: ComputerUseConfig | None = None,
) -> HostActionHandler:
    selected = config or (ComputerUseConfig(provider=backends[0].name) if backends else None)
    registration = ComputerUsePlugin(selected).pynchy_service_handler(tuple(backends))
    return registration.actions[0].handler


def test_computer_use_surface_and_provider_plugins_are_registered() -> None:
    with patch("pluggy.PluginManager.load_setuptools_entrypoints", return_value=0):
        pm = get_plugin_manager()

    names = {pm.get_name(plugin) for plugin in pm.get_plugins()}
    assert {
        "builtin-computer-use",
        "builtin-peekaboo",
        "builtin-cua-driver",
        "builtin-ssh-x11",
        "builtin-linux-x11",
    } <= names
    descriptor = get_host_action_catalog(pm).action_for("computer_use")
    assert descriptor is not None
    assert descriptor.capability.id == "desktop.computer.use"
    assert descriptor.capability.owner == "computer-use"
    assert len(descriptor.capability.action_ids) == 29
    assert descriptor.approval.mode is ApprovalMode.SESSION_TOOL


def test_provider_plugins_can_be_disabled_independently() -> None:
    with patch("pluggy.PluginManager.load_setuptools_entrypoints", return_value=0):
        pm = get_plugin_manager({"peekaboo": False})

    names = {pm.get_name(plugin) for plugin in pm.get_plugins()}
    assert "builtin-computer-use" in names
    assert "builtin-peekaboo" not in names
    assert "builtin-cua-driver" in names


@pytest.mark.asyncio
async def test_service_uses_only_the_configured_provider() -> None:
    unavailable = _RecordingBackend("peekaboo", False, "not installed")
    selected = _RecordingBackend("cua-driver")
    result = await _handler(
        unavailable,
        selected,
        config=ComputerUseConfig(provider="cua-driver"),
    )({"source_group": "admin", "action": "list_apps"})

    assert result["result"]["backend"] == "cua-driver"
    assert unavailable.requests == []
    assert [request.action.value for request in selected.requests] == ["list_apps"]


@pytest.mark.asyncio
async def test_service_accepts_canonical_ipc_transport_fields() -> None:
    backend = _RecordingBackend("peekaboo")

    result = await _handler(backend)(
        {
            "type": "service:computer_use",
            "request_id": "request-123",
            "source_group": "admin",
            "reply_to": "responses",
            "deadline": None,
            "action": "list_apps",
        }
    )

    assert result["result"]["backend"] == "peekaboo"
    assert len(backend.requests) == 1
    assert backend.requests[0].reply_to == "responses"
    assert backend.requests[0].deadline is None


@pytest.mark.asyncio
async def test_service_ignores_unselected_available_provider() -> None:
    peekaboo = _RecordingBackend("peekaboo")
    cua = _RecordingBackend("cua-driver")
    config = ComputerUseConfig(provider="cua-driver")
    result = await _handler(peekaboo, cua, config=config)(
        {"source_group": "admin", "action": "list_apps"}
    )

    assert result["result"]["backend"] == "cua-driver"
    assert peekaboo.requests == []


@pytest.mark.asyncio
async def test_service_does_not_retry_a_failed_mutation() -> None:
    selected = _FailingBackend("peekaboo")
    unselected = _RecordingBackend("cua-driver")

    result = await _handler(
        selected,
        unselected,
        config=ComputerUseConfig(provider="peekaboo"),
    )({"source_group": "admin", "action": "click", "coordinate": [10, 20]})

    assert result == {"error": "provider failed after starting the action"}
    assert [request.action.value for request in selected.requests] == ["click"]
    assert unselected.requests == []


def test_service_rejects_unknown_configured_provider() -> None:
    with pytest.raises(ValueError, match="provider 'missing' is not loaded"):
        ComputerUsePlugin(ComputerUseConfig(provider="missing")).pynchy_service_handler(
            (_RecordingBackend("peekaboo"),)
        )


@pytest.mark.asyncio
async def test_capability_probe_reports_unavailable_without_a_provider() -> None:
    unavailable = _RecordingBackend("peekaboo", False, "requires macOS")
    registration = ComputerUsePlugin(ComputerUseConfig(provider="peekaboo")).pynchy_service_handler(
        (unavailable,)
    )
    probe = registration.actions[0].capability.probe
    assert probe is not None

    result = await probe(CapabilityProbeContext(workspace="admin"))

    assert result.status is ProbeStatus.UNAVAILABLE
    assert result.reason == "computer-use provider peekaboo is unavailable: requires macOS"


@pytest.mark.action("desktop.computer.wait")
@pytest.mark.asyncio
async def test_wait_does_not_require_a_platform_provider() -> None:
    result = await _handler()({"source_group": "admin", "action": "wait", "seconds": 0})

    assert result == {"result": {"action": "wait", "backend": "host", "output": "waited 0s"}}


@pytest.mark.asyncio
async def test_capture_requires_lifecycle_data_directory() -> None:
    result = await _handler(
        _RecordingBackend("configured"),
        config=ComputerUseConfig(provider="configured"),
    )({"source_group": "admin", "action": "capture", "label": "shot"})

    assert result == {"error": "computer-use capture requires lifecycle configuration"}


@pytest.mark.asyncio
async def test_service_rejects_fields_outside_the_closed_contract() -> None:
    result = await _handler()(
        {"source_group": "admin", "action": "list_apps", "raw_cli_args": ["--dangerous"]}
    )

    assert "Extra inputs are not permitted" in result["error"]


@pytest.mark.parametrize("keys", [[], "+"])
@pytest.mark.asyncio
async def test_service_rejects_empty_shortcuts(keys: object) -> None:
    result = await _handler()({"source_group": "admin", "action": "key", "keys": keys})

    assert "key requires keys" in result["error"]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"source_group": "admin", "action": "click"}, "click actions require"),
        ({"source_group": "admin", "action": "type"}, "type requires text"),
        ({"source_group": "admin", "action": "scroll"}, "scroll requires direction"),
        ({"source_group": "admin", "action": "launch_app"}, "launch_app requires"),
        ({"source_group": "admin", "action": "launch_app", "app": " "}, "must not be empty"),
        ({"source_group": "../admin", "action": "list_apps"}, "one path component"),
        ({"source_group": "admin", "action": "set_value", "value": "x"}, "requires element"),
        (
            {"source_group": "admin", "action": "set_value", "element": "button"},
            "requires value",
        ),
        (
            {"source_group": "admin", "action": "perform_action", "accessibility_action": "press"},
            "requires element",
        ),
        (
            {"source_group": "admin", "action": "perform_action", "element": "button"},
            "requires accessibility_action",
        ),
        ({"source_group": "admin", "action": "menu_click"}, "menu_click requires"),
        ({"source_group": "admin", "action": "dialog_click"}, "dialog_click requires"),
        ({"source_group": "admin", "action": "dialog_input"}, "dialog_input requires"),
        ({"source_group": "admin", "action": "dialog_file"}, "dialog_file requires"),
        ({"source_group": "admin", "action": "clipboard_set"}, "clipboard_set requires"),
        ({"source_group": "admin", "action": "space_switch"}, "space_switch requires"),
        ({"source_group": "admin", "action": "space_move_window"}, "space_move_window requires"),
    ],
)
@pytest.mark.asyncio
async def test_service_rejects_invalid_request_boundaries(payload, message) -> None:
    result = await _handler()(payload)

    assert message in result["error"]


def test_service_rejects_duplicate_backend_catalog_entries() -> None:
    duplicate = _RecordingBackend("same")
    with pytest.raises(ValueError, match="duplicate computer-use provider: same"):
        ComputerUsePlugin().pynchy_service_handler((duplicate, duplicate))


def test_service_rejects_invalid_backend_catalog_entry() -> None:
    with (
        pytest.warns(UserWarning, match="violates type hint"),
        pytest.raises(TypeError, match="invalid provider"),
    ):
        ComputerUsePlugin().pynchy_service_handler((object(),))


@pytest.mark.action("desktop.computer.capture")
@pytest.mark.asyncio
async def test_service_uses_lifecycle_configuration(tmp_path: Path) -> None:
    backend = _RecordingBackend("configured")
    plugin = ComputerUsePlugin()
    plugin.configure(ComputerUseConfig(provider="configured"), data_dir=tmp_path)

    handler = plugin.pynchy_service_handler((backend,)).actions[0].handler
    result = await handler({"source_group": "admin", "action": "capture", "label": "shot"})

    assert result["result"]["backend"] == "configured"
    assert Path(result["result"]["output"]["screenshot_path"]).parent == (
        tmp_path / "ipc" / "admin" / "computer-use"
    )


@pytest.mark.asyncio
async def test_service_captures_after_a_non_capture_action(tmp_path: Path) -> None:
    backend = _RecordingBackend("configured")
    plugin = ComputerUsePlugin()
    plugin.configure(ComputerUseConfig(provider="configured"), data_dir=tmp_path)

    handler = plugin.pynchy_service_handler((backend,)).actions[0].handler
    result = await handler(
        {
            "source_group": "admin",
            "action": "list_apps",
            "capture_after": True,
        }
    )

    assert result["result"]["action"] == "list_apps"
    assert result["result"]["after"]["output"]["screenshot_path"].endswith("after-list-apps.png")
    assert [request.action.value for request in backend.requests] == ["list_apps", "capture"]


@pytest.mark.asyncio
async def test_capability_probe_reports_unconfigured_provider() -> None:
    registration = ComputerUsePlugin().pynchy_service_handler((_RecordingBackend("peekaboo"),))
    probe = registration.actions[0].capability.probe
    assert probe is not None

    result = await probe(CapabilityProbeContext(workspace="admin"))

    assert result.status is ProbeStatus.UNAVAILABLE
    assert result.reason == "computer-use provider is not configured"


@pytest.mark.asyncio
async def test_unconfigured_provider_rejects_non_wait_action() -> None:
    registration = ComputerUsePlugin().pynchy_service_handler((_RecordingBackend("peekaboo"),))

    result = await registration.actions[0].handler({"source_group": "admin", "action": "list_apps"})

    assert result == {"error": "computer-use provider is not configured"}


@pytest.mark.asyncio
async def test_capability_probe_reports_ready_primary_provider() -> None:
    registration = ComputerUsePlugin(ComputerUseConfig(provider="peekaboo")).pynchy_service_handler(
        (_RecordingBackend("peekaboo"),)
    )
    probe = registration.actions[0].capability.probe
    assert probe is not None

    result = await probe(CapabilityProbeContext(workspace="admin"))

    assert result.status is ProbeStatus.READY
    assert result.reason is None


def test_computer_use_plugin_returns_packaged_skill_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "is_dir", lambda _path: True)

    paths = ComputerUsePlugin().pynchy_skill_paths()

    assert len(paths) == 1
    assert paths[0].endswith("/skills/computer-use")


def test_computer_use_plugin_skips_missing_skill_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "is_dir", lambda _path: False)

    assert ComputerUsePlugin().pynchy_skill_paths() == []
