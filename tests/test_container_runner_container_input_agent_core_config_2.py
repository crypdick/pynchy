"""Tests for the container runner."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import (
    make_settings,
)

from pynchy.config.api import (
    AgentConfig,
    ProfileConfig,
    WorkspaceConfig,
    dynamic_thread_folder,
)
from pynchy.host.container_manager import session as session_mod
from pynchy.host.container_manager.api import McpStartupFailure
from pynchy.host.orchestrator.agent_runner import (
    PreContainerResult,
    build_container_input,
    run_agent,
)
from pynchy.identifiers import (
    GroupFolder,
)
from pynchy.workspace.api import (
    WorkspaceProfile,
)
from tests.container_runner_support import (
    _agent_runtime,
    _AgentRunnerDeps,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_GROUP = WorkspaceProfile(
    jid="test@g.us",
    name="Test Group",
    folder="test-group",
    trigger="@pynchy",
    added_at="2024-01-01T00:00:00.000Z",
)

_CR_CREDS = "pynchy.host.container_manager.credentials"
_CR_ORCH = "pynchy.host.container_manager.orchestrator"
_GATEWAY = "pynchy.host.container_manager.gateway"


_SETTINGS_MODULES = [
    "pynchy.host.orchestrator.workspace_config",
]

_test_settings: ContextVar[Any | None] = ContextVar("test_settings", default=None)


class TestContainerInputAgentCoreConfig:
    """Test model configuration passed from host settings into agent cores."""

    @staticmethod
    def _ctx(
        session_id: str | None = None,
        *,
        turn_id: str | None = None,
    ) -> PreContainerResult:
        return PreContainerResult(
            is_admin=False,
            repo_access=None,
            repo_accesses=[],
            system_prompt_append=None,
            session_id=session_id,
            system_notices=[],
            agent_core_module="agent_runner.cores.codex",
            agent_core_class="CodexCLIAgentCore",
            wrapped_on_output=AsyncMock(),
            config_timeout=30.0,
            snapshot_ms=0.0,
            turn_id=turn_id,
        )

    def test_invocation_reasoning_effort_overrides_workspace_effort(self):
        settings = make_settings(
            profiles={"code": ProfileConfig()},
            workspaces={
                TEST_GROUP.folder: WorkspaceConfig(
                    profiles=["code"],
                    model_reasoning_effort="ultra",
                )
            },
        )

        with patch(
            "pynchy.host.orchestrator.workspace_config.get_settings",
            return_value=settings,
        ):
            result = build_container_input(
                [],
                self._ctx(),
                "chat",
                TEST_GROUP,
                runtime=_agent_runtime(settings),
                model_reasoning_effort_override="medium",
            )

        assert result.agent_core_config is not None
        assert result.agent_core_config["model_reasoning_effort"] == "medium"

    @pytest.mark.asyncio
    async def test_scheduled_run_reuses_durable_host_session(self, tmp_path: Path):
        parent_folder = "host-group"
        child_folder = dynamic_thread_folder(parent_folder, "discord:daily-review")
        group = WorkspaceProfile(
            jid="discord:daily-review",
            name="Host Group/Daily Review",
            folder=child_folder,
            trigger="@pynchy",
            is_admin=True,
        )
        deps = _AgentRunnerDeps({child_folder: "interactive-session"})
        ctx = self._ctx("interactive-session")
        ctx.is_admin = True
        settings = make_settings(
            profiles={
                "host-admin": ProfileConfig(
                    is_admin=True,
                    execution_mode="host",
                    cwd=str(tmp_path),
                )
            },
            workspaces={parent_folder: WorkspaceConfig(profiles=["host-admin"])},
        )

        with (
            patch.object(deps, "agent_execution_runtime", _agent_runtime(settings)),
            patch(
                "pynchy.host.orchestrator.workspace_config.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.orchestrator.agent_runner.pre_container_setup",
                new_callable=AsyncMock,
                return_value=ctx,
            ),
            patch(
                "pynchy.host.orchestrator.host_agent_dispatch.prepare_host_codex_home",
                return_value=tmp_path / ".codex",
            ),
            patch(
                "pynchy.host.orchestrator.host_agent_dispatch.host_agent_env_vars",
                return_value={"CODEX_HOME": str(tmp_path / ".codex")},
            ),
            patch.object(
                deps.host_runtime_operations,
                "prepare_mcp",
                new_callable=AsyncMock,
            ),
            patch(
                "pynchy.host.orchestrator.host_agent_dispatch.codex_thread_exists_in_host_runtime",
                return_value=True,
            ),
            patch(
                "pynchy.host.orchestrator.host_agent_dispatch.run_host_agent_turn",
                new_callable=AsyncMock,
                return_value="success",
            ) as run_host_agent_turn,
            patch.object(
                deps.container_agent_operations,
                "destroy_session",
                new_callable=AsyncMock,
            ) as destroy_session,
            patch(
                "pynchy.host.orchestrator.agent_runner.clear_session",
                new_callable=AsyncMock,
            ) as clear_session,
        ):
            result = await run_agent(
                deps,
                group,
                "chat",
                [{"content": "review the prior day"}],
                is_scheduled_task=True,
            )

        assert result == "success"
        run_host_agent_turn.assert_awaited_once()
        input_data = run_host_agent_turn.await_args.args[0].input_data
        assert input_data.is_scheduled_task is True
        assert input_data.session_id == "interactive-session"
        destroy_session.assert_not_awaited()
        clear_session.assert_not_awaited()
        assert deps.sessions == {child_folder: "interactive-session"}

    @pytest.mark.asyncio
    async def test_scheduled_host_cancellation_preserves_recovery_state(self, tmp_path: Path):
        group = WorkspaceProfile(
            jid="host@g.us",
            name="Host Group",
            folder="host-group",
            trigger="@pynchy",
            is_admin=True,
        )
        deps = _AgentRunnerDeps({"host-group": "interrupted-session"})
        ctx = self._ctx("interrupted-session")
        settings = make_settings(
            profiles={
                "host-admin": ProfileConfig(
                    is_admin=True,
                    execution_mode="host",
                    cwd=str(tmp_path),
                )
            },
            workspaces={group.folder: WorkspaceConfig(profiles=["host-admin"])},
        )

        with (
            patch.object(deps, "agent_execution_runtime", _agent_runtime(settings)),
            patch(
                "pynchy.host.orchestrator.workspace_config.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.orchestrator.agent_runner.pre_container_setup",
                new_callable=AsyncMock,
                return_value=ctx,
            ),
            patch(
                "pynchy.host.orchestrator.agent_runner._run_host_execution",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError,
            ),
            patch.object(
                deps.container_agent_operations,
                "destroy_session",
                new_callable=AsyncMock,
            ) as destroy_session,
            patch(
                "pynchy.host.orchestrator.agent_runner.clear_session",
                new_callable=AsyncMock,
            ) as clear_session,
            pytest.raises(asyncio.CancelledError),
        ):
            await run_agent(
                deps,
                group,
                "chat",
                [{"content": "review the prior day"}],
                is_scheduled_task=True,
            )

        destroy_session.assert_not_awaited()
        clear_session.assert_not_awaited()
        assert deps.sessions == {"host-group": "interrupted-session"}

    @pytest.mark.asyncio
    async def test_host_execution_includes_ready_direct_mcp_servers(self, tmp_path: Path):
        group = WorkspaceProfile(
            jid="host@g.us",
            name="Host Group",
            folder="host-group",
            trigger="@pynchy",
            is_admin=True,
        )
        deps = _AgentRunnerDeps()
        ctx = self._ctx()
        ctx.is_admin = True
        settings = make_settings(
            profiles={
                "host-admin": ProfileConfig(
                    is_admin=True,
                    execution_mode="host",
                    cwd=str(tmp_path),
                )
            },
            workspaces={group.folder: WorkspaceConfig(profiles=["host-admin"])},
        )
        direct_servers = [
            {
                "name": "linear",
                "url": "http://192.168.64.1:9876/mcp/host-group/0/linear",
                "transport": "streamable_http",
            }
        ]

        async def prepare_mcp(input_data, _group_folder, _chat_jid, _broadcast_host_message):
            await asyncio.sleep(0)
            input_data.invocation_ts = 123.0
            input_data.mcp_direct_servers = direct_servers

        deps.host_runtime_operations.build_agent_environment = MagicMock(
            return_value={"OPENAI_BASE_URL": "http://192.168.64.1:4000"}
        )
        deps.host_runtime_operations.prepare_mcp = AsyncMock(side_effect=prepare_mcp)
        deps.host_runtime_operations.sessions_root = tmp_path / "sessions"
        deps.host_runtime_operations.project_root = tmp_path
        deps.host_runtime_operations.prepare_host_codex_home = lambda _folder, _plugins: (
            tmp_path / ".codex"
        )

        with (
            patch.object(deps, "agent_execution_runtime", _agent_runtime(settings)),
            patch(
                "pynchy.host.orchestrator.workspace_config.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.orchestrator.agent_runner.pre_container_setup",
                new_callable=AsyncMock,
                return_value=ctx,
            ),
            patch(
                "pynchy.host.orchestrator.host_execution.run_host_input",
                new_callable=AsyncMock,
                return_value="success",
            ) as run_host_input,
        ):
            result = await run_agent(deps, group, "chat", [{"content": "hi"}])

        assert result == "success"
        deps.host_runtime_operations.prepare_mcp.assert_awaited_once_with(
            run_host_input.await_args.args[0],
            "host-group",
            "chat",
            deps.broadcast_host_message,
        )
        assert run_host_input.await_args.args[0].mcp_direct_servers == direct_servers

    @pytest.mark.asyncio
    async def test_host_execution_clears_codex_session_missing_from_host_runtime(
        self, tmp_path: Path
    ):
        group = WorkspaceProfile(
            jid="host@g.us",
            name="Host Group",
            folder="host-group",
            trigger="@pynchy",
            is_admin=True,
        )
        deps = _AgentRunnerDeps({"host-group": "codex:gpt-5.5:missing-thread"})
        ctx = self._ctx("codex:gpt-5.5:missing-thread")
        ctx.is_admin = True
        settings = make_settings(
            profiles={
                "host-admin": ProfileConfig(
                    is_admin=True,
                    execution_mode="host",
                    cwd=str(tmp_path),
                )
            },
            workspaces={group.folder: WorkspaceConfig(profiles=["host-admin"])},
            agent=AgentConfig(model="gpt-5.5"),
        )

        with (
            patch.object(deps, "agent_execution_runtime", _agent_runtime(settings)),
            patch(
                "pynchy.host.orchestrator.workspace_config.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.orchestrator.agent_runner.pre_container_setup",
                new_callable=AsyncMock,
                return_value=ctx,
            ),
            patch(
                "pynchy.host.orchestrator.host_agent_dispatch.prepare_host_codex_home",
                return_value=tmp_path / ".codex",
            ),
            patch(
                "pynchy.host.orchestrator.host_agent_dispatch.codex_thread_exists_in_host_runtime",
                return_value=False,
            ) as migrate_thread,
            patch(
                "pynchy.host.orchestrator.host_agent_dispatch.host_agent_env_vars",
                return_value={"CODEX_HOME": str(tmp_path / ".codex")},
            ),
            patch(
                "pynchy.host.orchestrator.host_execution.run_host_input",
                new_callable=AsyncMock,
                return_value="success",
            ) as run_host_input,
            patch.object(
                deps.container_agent_operations,
                "destroy_session",
                new_callable=AsyncMock,
            ) as destroy_session,
            patch(
                "pynchy.host.orchestrator.host_agent_dispatch.clear_runtime_session_references",
                new_callable=AsyncMock,
            ) as clear_runtime_session_references,
        ):
            result = await run_agent(deps, group, "chat", [{"content": "hi"}])

        assert result == "success"
        input_data = run_host_input.await_args.args[0]
        assert input_data.session_id is None
        assert deps.sessions == {}
        destroy_session.assert_awaited_once_with(group.folder)
        clear_runtime_session_references.assert_awaited_once_with(
            GroupFolder(group.folder),
            "codex:gpt-5.5:missing-thread",
            "chat",
        )
        migrate_thread.assert_called_once_with(
            "codex:gpt-5.5:missing-thread",
            codex_home=tmp_path / ".codex",
        )

    @pytest.mark.asyncio
    async def test_warm_turn_refreshes_personalized_skills_before_ipc(self):
        """Canonical skill updates synchronize before a persistent container's next turn."""
        deps = _AgentRunnerDeps()
        ctx = self._ctx()
        session = session_mod.ContainerSession(TEST_GROUP.folder, "pynchy-test-group")
        session.proc = MagicMock(spec=asyncio.subprocess.Process)
        session.proc.returncode = None
        session.send_ipc_message = AsyncMock()
        settings = make_settings()

        with (
            patch.object(deps, "agent_execution_runtime", _agent_runtime(settings)),
            patch(
                "pynchy.host.orchestrator.workspace_config.get_settings",
                return_value=settings,
            ),
            patch(
                "pynchy.host.orchestrator.agent_runner.pre_container_setup",
                new_callable=AsyncMock,
                return_value=ctx,
            ),
            patch.object(deps.container_agent_operations, "get_session", return_value=session),
            patch.object(
                deps.container_agent_operations,
                "ensure_workspace_mcp",
                new=AsyncMock(
                    return_value=(
                        McpStartupFailure(
                            instance_id="calendar",
                            server_name="calendar",
                            reason="start timed out",
                        ),
                    )
                ),
            ),
            patch.object(deps, "broadcast_host_message", new=AsyncMock()) as broadcast,
            patch.object(deps, "refresh_personalized_agent_skills") as refresh_skills,
            patch.object(
                session,
                "set_output_handler",
                wraps=session.set_output_handler,
            ) as set_output_handler,
            patch(
                "pynchy.host.orchestrator.agent_runner._await_query",
                new_callable=AsyncMock,
                return_value="success",
            ),
        ):
            result = await run_agent(
                deps,
                TEST_GROUP,
                "chat",
                [{"content": "follow up"}],
                resume_session_id="resume-session",
            )

        assert result == "success"
        refresh_skills.assert_called_once_with(TEST_GROUP.folder)
        broadcast.assert_awaited_once()
        session.send_ipc_message.assert_awaited_once()
        sent_query_id = session.send_ipc_message.await_args.kwargs["query_id"]
        assert set_output_handler.call_args.kwargs["query_id"] == sent_query_id

    @pytest.mark.asyncio
    async def test_cold_start_notifies_mcp_failures_before_query(self):
        deps = _AgentRunnerDeps()
        ctx = self._ctx()
        session = MagicMock()
        operations = replace(
            deps.container_agent_operations,
            fresh_container_name=AsyncMock(return_value="pynchy-test-group"),
            spawn=AsyncMock(
                return_value=(
                    MagicMock(),
                    "pynchy-test-group",
                    [],
                    (
                        McpStartupFailure(
                            instance_id="calendar",
                            server_name="calendar",
                            reason="start timed out",
                        ),
                    ),
                )
            ),
            create_session=AsyncMock(return_value=session),
        )
        deps.container_agent_operations = operations

        with (
            patch(
                "pynchy.host.orchestrator.agent_runner.pre_container_setup",
                new_callable=AsyncMock,
                return_value=ctx,
            ),
            patch(
                "pynchy.host.orchestrator.agent_runner._await_query",
                new_callable=AsyncMock,
                return_value="success",
            ),
            patch.object(deps, "broadcast_host_message", new=AsyncMock()) as broadcast,
        ):
            result = await run_agent(deps, TEST_GROUP, "chat", [{"content": "start"}])

        assert result == "success"
        broadcast.assert_awaited_once()
