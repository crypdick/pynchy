"""Focused Gog capability-probe edge contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pynchy.plugins.api import CapabilityProbeContext, ProbeStatus
from pynchy.plugins.integrations import gog

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_gog_executable_probe_uses_path_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "gog"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    gog.configure_gog_runtime(
        gog.GogRuntime(
            config=gog.GogConfig(command="gog", account="you@example.com"),
            home=tmp_path,
            oauth_client_path=None,
            workspace_enables_gog=lambda _workspace: True,
        )
    )
    action = gog.GOG_HOST_ACTIONS.action_for("gog_gmail_search")
    assert action is not None
    assert action.capability.probe is not None

    result = await action.capability.probe(CapabilityProbeContext("workspace"))

    assert result.status is ProbeStatus.READY
