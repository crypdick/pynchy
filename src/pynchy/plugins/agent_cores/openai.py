"""Built-in OpenAI Agents SDK agent core plugin.

This plugin provides the OpenAI Agents SDK agent core as an alternative to
Claude SDK. It's registered automatically during plugin auto-discovery.

Activate with: PYNCHY_AGENT_CORE=openai
Requires: OPENAI_API_KEY environment variable in the container.
"""

from __future__ import annotations

import pluggy

hookimpl = pluggy.HookimplMarker("pynchy")


class OpenAIAgentCorePlugin:
    """Built-in plugin for OpenAI Agents SDK agent core.

    The implementation lives in src/pynchy/agent/agent_runner/src/agent_runner/cores/openai.py
    and is baked into the container image alongside the Claude core.
    """

    @hookimpl
    def pynchy_agent_core_info(self) -> dict[str, str | list[str] | None]:
        """Provide OpenAI agent core information."""
        return {
            "name": "openai",
            "module": "agent_runner.cores.openai",
            "class_name": "OpenAIAgentCore",
            "packages": ["openai-agents>=0.1.0"],
            "host_source_path": None,
        }
