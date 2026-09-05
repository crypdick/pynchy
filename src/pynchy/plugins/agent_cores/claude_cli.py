"""Built-in Claude Code CLI agent core plugin.

Advertises the ``claude-cli`` agent core, which drives the ``claude`` binary as
a subprocess over the stream-json protocol instead of the Claude Agent SDK.
Select it with ``[agent] default_core = "claude-cli"`` (or
``AGENT__DEFAULT_CORE=claude-cli``).
The implementation lives in
src/pynchy/agent/agent_runner/src/agent_runner/cores/claude_cli.py and is
already baked into the container image (the ``claude`` CLI is installed there).
"""

from __future__ import annotations

import pluggy

from pynchy.plugins.api import AgentCoreSpec

hookimpl = pluggy.HookimplMarker("pynchy")


class ClaudeCLIAgentCorePlugin:  # noqa: V102
    """Built-in plugin for the Claude Code CLI agent core.

    Unlike the default ``claude`` core (Claude Agent SDK), this core owns the
    CLI subprocess and its stdout parse loop, giving turn-by-turn control and a
    seam to inject arbitrary customizations into the token stream.
    """

    @hookimpl
    def pynchy_agent_core_info(self) -> AgentCoreSpec:
        """Provide Claude CLI agent core information."""
        return AgentCoreSpec(
            name="claude-cli",
            module="agent_runner.cores.claude_cli",
            class_name="ClaudeCLIAgentCore",
        )
