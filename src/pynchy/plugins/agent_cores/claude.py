"""Built-in Claude SDK agent core plugin.

This plugin provides the default Claude SDK agent core. It's registered
automatically during plugin discovery and requires no installation.
"""

from __future__ import annotations

import pluggy

hookimpl = pluggy.HookimplMarker("pynchy")


class ClaudeAgentCorePlugin:
    """Built-in plugin for Claude SDK agent core.

    This is the default agent core that uses Anthropic's Claude Agent SDK.
    The implementation lives in src/pynchy/agent/agent_runner/src/agent_runner/cores/claude.py
    and is already baked into the container image.
    """

    @hookimpl
    def pynchy_agent_core_info(self) -> dict[str, str | list[str] | None]:
        """Provide Claude agent core information."""
        return {
            "name": "claude",
            "module": "agent_runner.cores.claude",
            "class_name": "ClaudeAgentCore",
            "packages": [],
            "host_source_path": None,
        }
