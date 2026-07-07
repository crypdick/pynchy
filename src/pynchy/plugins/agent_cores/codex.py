"""Built-in OpenAI Codex CLI agent core plugin.

Advertises the ``codex`` agent core, which drives the OpenAI Codex CLI via
``codex exec --json``. Select it with ``[agent] core = "codex"`` (or
``PYNCHY_AGENT_CORE=codex``). This core routes Codex model traffic through
Pynchy's OpenAI API gateway.
"""

from __future__ import annotations

import pluggy

hookimpl = pluggy.HookimplMarker("pynchy")


class CodexAgentCorePlugin:
    """Built-in plugin for the Codex CLI agent core."""

    @hookimpl
    def pynchy_agent_core_info(self) -> dict[str, str | list[str] | None]:
        """Provide Codex CLI agent core information."""
        return {
            "name": "codex",
            "module": "agent_runner.cores.codex",
            "class_name": "CodexCLIAgentCore",
            "packages": [],
            "host_source_path": None,
        }
