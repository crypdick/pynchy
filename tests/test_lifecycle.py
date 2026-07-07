"""Lifecycle startup regression tests."""

from __future__ import annotations

import pytest

from pynchy.host.orchestrator import lifecycle
from pynchy.host.orchestrator.app import PynchyApp


class StopAfterArgumentValidation(Exception):
    """Sentinel raised once run_app reaches its first startup phase."""


@pytest.mark.asyncio
async def test_run_app_resolves_pynchyapp_runtime_annotation(monkeypatch):
    async def stop_before_startup(app: PynchyApp) -> None:
        raise StopAfterArgumentValidation

    monkeypatch.setattr(lifecycle, "_initialize_core", stop_before_startup)

    with pytest.raises(StopAfterArgumentValidation):
        await lifecycle.run_app(PynchyApp())
