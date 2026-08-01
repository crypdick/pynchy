"""Public fallback behavior for agent-core configuration resolution."""

from pynchy.host.orchestrator.agent_core_config import agent_core_config


def test_agent_core_config_without_workspace_uses_global_values() -> None:
    assert agent_core_config("global-model", "medium") == {
        "model": "global-model",
        "model_reasoning_effort": "medium",
    }
