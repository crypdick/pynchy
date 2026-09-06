"""Built-in MCP entry points use the host installation, independent of checkout state."""

from __future__ import annotations

import subprocess  # noqa: S404 - exercise the trusted plugin launch spec at the process boundary.
from typing import TYPE_CHECKING

import pytest

from pynchy.config.api import LinearTool
from pynchy.plugins.integrations.api import LinearAccount
from pynchy.plugins.integrations.linear import LinearMcpPlugin
from pynchy.plugins.integrations.proton_mail import ProtonMailMcpPlugin

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("provider", ["linear", "proton-mail"])
def test_builtin_mcp_launch_ignores_unusable_checkout(tmp_path: Path, provider: str):
    # A partial checkout must not resolve dependencies or select another Python.
    (tmp_path / "pyproject.toml").write_text("[project\n", encoding="utf-8")
    if provider == "linear":
        spec = LinearMcpPlugin(
            (LinearAccount("linear", LinearTool(type="linear")),)
        ).pynchy_mcp_server_spec()[0]
    else:
        spec = ProtonMailMcpPlugin().pynchy_mcp_server_spec()[0]
    assert spec.config.command is not None
    args = [
        argument.replace("{port}", "0").replace("{workspace}", "test")
        for argument in spec.config.args
    ]
    result = subprocess.run(  # noqa: S603 - execute the built-in plugin's trusted public launch spec.
        [spec.config.command, *args, "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--port" in result.stdout
