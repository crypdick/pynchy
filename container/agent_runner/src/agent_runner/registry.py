"""Agent core registry for discovering and instantiating agent implementations.

Cores are registered in two ways:
1. Built-in cores (lazy imports to handle missing dependencies gracefully)
2. Third-party cores via entry points (pynchy.agent_cores group)
"""

from __future__ import annotations

import sys

from .core import AgentCore, AgentCoreConfig

_CORE_REGISTRY: dict[str, type] = {}


def register_core(name: str, cls: type) -> None:
    """Register an agent core implementation.

    Args:
        name: Core identifier (e.g., "claude", "openai")
        cls: Class implementing AgentCore protocol
    """
    _CORE_REGISTRY[name] = cls


def create_agent_core(name: str, config: AgentCoreConfig) -> AgentCore:
    """Create an agent core instance by name.

    Args:
        name: Registered core name
        config: Core configuration

    Returns:
        Instantiated AgentCore implementation

    Raises:
        KeyError: If core name is not registered
        TypeError: If core doesn't satisfy AgentCore protocol
    """
    if name not in _CORE_REGISTRY:
        available = ", ".join(_CORE_REGISTRY.keys())
        raise KeyError(f"Unknown agent core '{name}'. Available cores: {available}")

    cls = _CORE_REGISTRY[name]
    instance = cls(config)

    # Runtime protocol check
    if not isinstance(instance, AgentCore):
        raise TypeError(f"Core '{name}' does not satisfy AgentCore protocol")

    return instance


def list_cores() -> list[str]:
    """Return list of registered core names."""
    return list(_CORE_REGISTRY.keys())


def _register_built_in_cores() -> None:
    """Register built-in cores with lazy imports.

    Lazy imports allow missing SDKs (e.g., claude-agent-sdk not installed)
    to fail gracefully rather than crashing the entire registry.
    """
    # Claude SDK
    try:
        from .cores.claude import ClaudeAgentCore

        register_core("claude", ClaudeAgentCore)
    except ImportError:
        # Claude SDK not available, skip registration
        pass


def _discover_entry_point_cores() -> None:
    """Discover and register third-party cores via entry points.

    Third-party cores register themselves via pyproject.toml:

    [project.entry-points."pynchy.agent_cores"]
    openai = "pynchy_core_openai.core:OpenAIAgentCore"
    """
    try:
        # Python 3.10+ preferred API
        from importlib.metadata import entry_points
    except ImportError:
        # Python 3.9 fallback
        try:
            from importlib_metadata import entry_points  # type: ignore
        except ImportError:
            # No entry points library available
            return

    # Load entry points from pynchy.agent_cores group
    try:
        # Python 3.10+ returns EntryPoints (dict-like), 3.9 returns dict
        eps = entry_points()
        if hasattr(eps, "select"):
            # Python 3.10+
            group = eps.select(group="pynchy.agent_cores")
        else:
            # Python 3.9
            group = eps.get("pynchy.agent_cores", [])

        for ep in group:
            try:
                cls = ep.load()
                register_core(ep.name, cls)
            except Exception as exc:
                print(
                    f"[agent-runner] Failed to load core '{ep.name}': {exc}",
                    file=sys.stderr,
                )
    except Exception as exc:
        print(f"[agent-runner] Entry point discovery failed: {exc}", file=sys.stderr)


# Auto-register at import time
_register_built_in_cores()
_discover_entry_point_cores()
