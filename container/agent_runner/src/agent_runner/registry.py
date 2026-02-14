"""Agent core registry for discovering and instantiating agent implementations.

The host orchestrates agent core selection via the plugin system. The host
discovers plugins, calls their methods to get module/class info, and passes
that info to the container via ContainerInput. The container imports and
instantiates the core class directly.
"""

from __future__ import annotations

import importlib

from .core import AgentCore, AgentCoreConfig


def create_agent_core(module_path: str, class_name: str, config: AgentCoreConfig) -> AgentCore:
    """Create an agent core instance by importing and instantiating directly.

    Args:
        module_path: Fully qualified module path (e.g., "agent_runner.cores.claude")
        class_name: Class name within the module (e.g., "ClaudeAgentCore")
        config: Core configuration

    Returns:
        Instantiated AgentCore implementation

    Raises:
        ImportError: If module cannot be imported
        AttributeError: If class doesn't exist in module
        TypeError: If core doesn't satisfy AgentCore protocol
    """
    try:
        # Import the module
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise ImportError(f"Failed to import agent core module '{module_path}': {exc}") from exc

    # Get the class from the module
    try:
        cls = getattr(module, class_name)
    except AttributeError as exc:
        raise AttributeError(f"Module '{module_path}' has no class '{class_name}'") from exc

    # Instantiate the core
    try:
        instance = cls(config)
    except Exception as exc:
        raise TypeError(f"Failed to instantiate {module_path}.{class_name}: {exc}") from exc

    # Runtime protocol check
    if not isinstance(instance, AgentCore):
        raise TypeError(f"Class {module_path}.{class_name} does not satisfy AgentCore protocol")

    return instance
