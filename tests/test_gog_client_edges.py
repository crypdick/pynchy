"""Focused Gog capability-probe edge contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pynchy.plugins.api import CapabilityProbeContext, ProbeStatus
from pynchy.plugins.integrations import gog

if TYPE_CHECKING:
    from pathlib import Path


def test_gog_config_normalizes_optional_none_values() -> None:
    config = gog.GogConfig(account=None, home=None, oauth_client_path=None)

    assert config.account is None
    assert config.home is None
    assert config.oauth_client_path is None


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("command", "gog\n--version", "command must be one executable"),
        ("account", "user\n@example.com", "account must be a single"),
        ("home", "data\x00gog", "path must be non-empty"),
        ("oauth_client_path", "data\x00client.json", "path must be non-empty"),
    ],
)
def test_gog_config_rejects_unsafe_single_line_values(field: str, value: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        gog.GogConfig(**{field: value})


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
