"""Denied host tools stay outside the agent-visible tool catalog."""

from unittest.mock import AsyncMock, patch

import pytest
from conftest import make_host_action_catalog

from pynchy.host.orchestrator.agent_runner import PreContainerSetupRequest, pre_container_setup
from pynchy.state.api import SessionSecurityTaint
from pynchy.workspace.api import (
    CapabilityRule,
    ResolvedToolAccess,
    ResolvedWorkspaceConfig,
    WorkspaceProfile,
)
from tests.container_runner_support import _AgentRunnerDeps

_GROUP = WorkspaceProfile(
    jid="test@g.us",
    name="Test Group",
    folder="test-group",
    trigger="@pynchy",
)


@pytest.mark.asyncio
async def test_pre_container_setup_hides_explicitly_denied_host_tool() -> None:
    deps = _AgentRunnerDeps()
    resolved = ResolvedWorkspaceConfig(
        skills=[],
        tools=["computer_use", "linear", "safe_tool"],
        repo=[],
        model=None,
        execution_mode="container",
        cwd=None,
        is_admin=False,
        contains_secrets=False,
        capabilities={"test.computer.use": CapabilityRule("deny")},
    )
    access = ResolvedToolAccess(
        tools=("computer_use", "linear", "safe_tool"),
        companion_skills=(),
        workspace_env={},
        missing_requirements={},
        agent_tool_grants=("computer_use", "linear", "safe_tool"),
    )
    with (
        patch(
            "pynchy.host.orchestrator._agent_runner_preflight.get_session_security_taint",
            new_callable=AsyncMock,
            return_value=SessionSecurityTaint(),
        ),
        patch(
            "pynchy.host.orchestrator._agent_runner_preflight.workspace_config.load_resolved_config",
            return_value=resolved,
        ),
        patch(
            "pynchy.host.orchestrator._agent_runner_preflight.workspace_config.prompt_ids_for_context",
            return_value=["executor/default"],
        ),
        patch(
            "pynchy.host.orchestrator._agent_runner_preflight.workspace_config.read_prompts",
            return_value="executor prompt",
        ),
        patch(
            "pynchy.host.orchestrator._agent_runner_preflight.write_container_snapshots",
            new_callable=AsyncMock,
            return_value=1.0,
        ),
        patch(
            "pynchy.host.orchestrator._agent_runner_preflight.workspace_config.load_resolved_tool_access",
            return_value=access,
        ),
        patch(
            "pynchy.host.orchestrator._agent_runner_preflight.get_host_action_catalog",
            return_value=make_host_action_catalog("computer_use", "safe_tool", handler=AsyncMock()),
        ),
        patch(
            "pynchy.host.orchestrator._agent_runner_preflight.resolve_agent_core",
            return_value=("agent_runner.cores.claude", "ClaudeAgentCore"),
        ),
        patch(
            "pynchy.host.orchestrator._agent_runner_preflight.resolve_container_timeout",
            return_value=30.0,
        ),
    ):
        result = await pre_container_setup(
            PreContainerSetupRequest(
                deps=deps,
                group=_GROUP,
                chat_jid="test@g.us",
                messages=[{"content": "hello"}],
                on_output=None,
                extra_system_notices=None,
                input_source="user",
                is_scheduled_task=False,
                repo_access_override=None,
                runtime=deps.agent_execution_runtime,
            )
        )

    assert result.agent_tool_grants == ("linear", "safe_tool")
