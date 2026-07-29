"""Security tool projection boundary contracts."""

from __future__ import annotations

from pynchy.config.api import validate_settings_mapping
from pynchy.host.container_manager.security.gate import build_workspace_security


def test_build_workspace_security_ignores_unresolved_tool_names() -> None:
    settings = validate_settings_mapping(
        {
            "profiles": {"worker": {"tools": ["missing-tool"]}},
            "workspaces": {"research": {"profiles": ["worker"]}},
            "tools": {"missing-tool": {"type": "workspace"}},
        }
    )

    resolved = settings.resolved_workspace_config("research")
    assert resolved is not None

    security = build_workspace_security(settings, resolved)

    assert security.services == {}
