"""Built-in OpenAI Codex CLI agent core plugin.

Advertises the ``codex`` agent core, which drives the OpenAI Codex CLI via
``codex exec --json``. Select it with ``[agent] default_core = "codex"`` (or
``AGENT__DEFAULT_CORE=codex``). This core routes Codex model traffic through
Pynchy's OpenAI API gateway.
"""

from __future__ import annotations

import pluggy

from pynchy.plugins.api import AgentCoreSpec

hookimpl = pluggy.HookimplMarker("pynchy")


class CodexAgentCorePlugin:  # noqa: V102
    """Built-in plugin for the Codex CLI agent core."""

    @hookimpl
    def pynchy_agent_core_info(self) -> AgentCoreSpec:
        """Provide Codex CLI agent core information."""
        return AgentCoreSpec(
            name="codex",
            module="agent_runner.cores.codex",
            class_name="CodexCLIAgentCore",
        )
