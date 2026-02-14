"""Agent core plugin infrastructure.

Agent core plugins provide alternative LLM agent frameworks (OpenAI, Ollama,
LangChain, etc.) that can be swapped in place of the default Claude SDK.
"""

from __future__ import annotations

from abc import abstractmethod

from .base import PluginBase


class AgentCorePlugin(PluginBase):
    """Base class for agent core plugins.

    Agent core plugins provide alternative LLM agent frameworks. The plugin
    class runs on the host to provide metadata and installation info. The
    actual core implementation runs inside the container and is discovered
    via the container-side registry.

    ## Security Profile

    **Risk level: Medium** — Plugin class runs on host during discovery, but
    the actual agent core implementation runs inside the container sandbox.

    The plugin's ``core_name()`` and ``container_packages()`` methods execute
    on the host with full access. The core implementation itself (the class
    that satisfies the AgentCore protocol) runs inside the container.

    Installing an agent core plugin means trusting:
    - The plugin class code (host execution)
    - The core implementation (container execution)
    - Any additional packages it installs in the container

    ## Container Registration

    The core implementation must register itself via entry points or be
    importable by the container-side registry in ``agent_runner/registry.py``.

    For third-party cores, add to your plugin's ``pyproject.toml``:

    ```toml
    [project.entry-points."pynchy.agent_cores"]
    openai = "pynchy_core_openai.core:OpenAIAgentCore"
    ```

    The container will discover and register the core automatically.
    """

    categories = ["agent_core"]

    @abstractmethod
    def core_name(self) -> str:
        """Return the core name used in the container-side registry.

        This must match the name registered in the container's agent_runner.registry.

        Returns:
            Core identifier (e.g., "openai", "ollama", "langchain")
        """
        ...

    def container_packages(self) -> list[str]:
        """Additional pip packages to install in the container.

        Override to specify dependencies needed by the core implementation.

        Returns:
            List of pip package specifications (e.g., ["openai>=1.0.0"])
        """
        return []

    def core_module_path(self) -> str | None:
        """Path to the core implementation module for container import.

        If the core is not installed via entry points, return the path to
        the Python module containing the core class. The plugin system will
        mount this path into the container for import.

        Returns:
            Absolute path to module directory, or None if using entry points
        """
        return None
