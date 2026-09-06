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
)
from pynchy.host.git_ops.api import RepoContext, WorktreeResult
from pynchy.host.orchestrator import host_execution
from pynchy.host.orchestrator.agent_runner import (
    PreContainerResult,
    build_container_input,
    run_agent,
)
from pynchy.state import SessionSecurityTaint
from pynchy.workspace.api import (
    RuntimeTarget,
    WorkspaceProfile,
)
from tests.container_runner_support import (
    _agent_runtime,
    _AgentRunnerDeps,
    _patch_settings,
    _profile_workspace,
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
        agent_tool_grants: tuple[str, ...] = (),
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
            agent_tool_grants=agent_tool_grants,
        )

    def test_agent_runner_queue_double_satisfies_host_process_contract(self) -> None:
        assert isinstance(_AgentRunnerDeps().queue, host_execution.HostProcessQueue)

    def test_agent_model_settings_flow_to_core_config(self):
        settings = make_settings(
            agent=AgentConfig(
                model="chatgpt/gpt-5.3-codex",
                model_reasoning_effort="ultra",
            )
        )

        result = build_container_input(
            [], self._ctx(), "chat", TEST_GROUP, runtime=_agent_runtime(settings)
        )

        assert result.agent_core_config is not None
        assert result.agent_core_config["model"] == "chatgpt/gpt-5.3-codex"
        assert result.agent_core_config["model_reasoning_effort"] == "ultra"
        assert result.agent_core_config["metadata"]["pynchy_turn_id"].startswith("turn_")

    def test_default_agent_model_flows_to_core_config(self):
        settings = make_settings()

        result = build_container_input(
            [], self._ctx(), "chat", TEST_GROUP, runtime=_agent_runtime(settings)
        )

        assert result.agent_core_config is not None
        assert "model" not in result.agent_core_config

    def test_agent_tool_grants_flow_to_agent_runner(self):
        result = build_container_input(
            [],
            self._ctx(agent_tool_grants=("linear", "computer_use")),
            "chat",
            TEST_GROUP,
            runtime=_agent_runtime(make_settings()),
        )

        assert result.agent_tool_grants == ["linear", "computer_use"]

    def test_missing_workspace_config_keeps_global_model(self):
        settings = make_settings(agent=AgentConfig(model="chatgpt/gpt-5.3-codex"))

        with patch(
            "pynchy.host.orchestrator.workspace_config.load_resolved_config",
            return_value=None,
        ):
            result = build_container_input(
                [], self._ctx(), "chat", TEST_GROUP, runtime=_agent_runtime(settings)
            )

        assert result.agent_core_config is not None
        assert result.agent_core_config["model"] == "chatgpt/gpt-5.3-codex"

    def test_turn_id_flows_to_container_input_and_core_metadata(self):
        settings = make_settings()

        result = build_container_input(
            [], self._ctx(turn_id="turn_1"), "chat", TEST_GROUP, runtime=_agent_runtime(settings)
        )

        assert result.turn_id == "turn_1"
        assert result.agent_core_config is not None
        assert result.agent_core_config["metadata"] == {
            "pynchy_turn_id": "turn_1",
            "pynchy_chat_jid": "chat",
            "pynchy_group_folder": TEST_GROUP.folder,
        }

    def test_workspace_model_overrides_global_agent_model(self):
        profiles, workspace = _profile_workspace(
            "codex-workspace",
            model="chatgpt/gpt-5.3-codex-spark",
        )
        settings = make_settings(
            agent=AgentConfig(model="chatgpt/gpt-5.3-codex"),
            profiles=profiles,
            workspaces={TEST_GROUP.folder: workspace},
        )

        with patch(
            "pynchy.host.orchestrator.workspace_config.get_settings",
            return_value=settings,
        ):
            result = build_container_input(
                [], self._ctx(), "chat", TEST_GROUP, runtime=_agent_runtime(settings)
            )

        assert result.agent_core_config is not None
        assert result.agent_core_config["model"] == "chatgpt/gpt-5.3-codex-spark"
        assert result.agent_core_config["metadata"]["pynchy_turn_id"].startswith("turn_")

    def test_direct_workspace_model_overrides_profile_and_global_model(self):
        profiles, workspace = _profile_workspace(
            "codex-workspace",
            model="chatgpt/gpt-5.3-codex-spark",
        )
        workspace = WorkspaceConfig(
            profiles=workspace.profiles,
            model="chatgpt/gpt-5.3-codex",
        )
        settings = make_settings(
            agent=AgentConfig(model="chatgpt/gpt-5.3-codex-mini"),
            profiles=profiles,
            workspaces={TEST_GROUP.folder: workspace},
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
                is_scheduled_task=True,
            )

        assert result.agent_core_config is not None
        assert result.agent_core_config["model"] == "chatgpt/gpt-5.3-codex"
        assert result.is_scheduled_task is True
        assert result.agent_core_config["metadata"]["pynchy_turn_id"].startswith("turn_")

    def test_workspace_reasoning_effort_overrides_global_effort(self):
        settings = make_settings(
            agent=AgentConfig(
                model="gpt-5.6-terra",
                model_reasoning_effort="ultra",
            ),
            profiles={"code": ProfileConfig()},
            workspaces={
                TEST_GROUP.folder: WorkspaceConfig(
                    profiles=["code"],
                    model_reasoning_effort="medium",
                )
            },
        )

        with patch(
            "pynchy.host.orchestrator.workspace_config.get_settings",
            return_value=settings,
        ):
            result = build_container_input(
                [], self._ctx(), "chat", TEST_GROUP, runtime=_agent_runtime(settings)
            )

        assert result.agent_core_config is not None
        assert result.agent_core_config["model_reasoning_effort"] == "medium"

    @pytest.mark.asyncio
    async def test_scheduled_repo_override_avoids_unselected_workspace_repo(self, tmp_path: Path):
        """A public scheduled run provisions only its explicit repository scope."""
        selected_slug = "owner/pynchy"
        unavailable_slug = "owner/private-tools"
        repo_root = tmp_path / "repos" / "pynchy"
        worktree_path = tmp_path / "worktrees" / "pynchy"
        (repo_root / ".git").mkdir(parents=True)
        worktree_path.mkdir(parents=True)
        repo_ctx = RepoContext(
            slug=selected_slug,
            root=repo_root,
            worktrees_dir=tmp_path / "worktrees",
        )
        profiles = {
            "multi-repo": ProfileConfig(repo=[selected_slug, unavailable_slug]),
        }
        workspaces = {
            TEST_GROUP.folder: WorkspaceConfig(profiles=["multi-repo"]),
        }
        deps = _AgentRunnerDeps()
        session = MagicMock()
        runtime = MagicMock(cli="docker")
        runtime.name = "docker"
        deps.container_agent_operations = replace(
            deps.container_agent_operations,
            start_session=AsyncMock(return_value=(session, ())),
            destroy_session=AsyncMock(),
        )

        def selected_repo_context(slug: str) -> RepoContext:
            if slug == unavailable_slug:
                raise PermissionError("unselected repository is inaccessible")
            assert slug == selected_slug
            return repo_ctx

        with (
            _patch_settings(
                tmp_path,
                profiles=profiles,
                workspaces=workspaces,
            ) as settings,
            patch.object(deps, "agent_execution_runtime", _agent_runtime(settings)),
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.write_container_snapshots",
                new_callable=AsyncMock,
                return_value=0.0,
            ),
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.get_session_security_taint",
                new_callable=AsyncMock,
                return_value=SessionSecurityTaint(),
            ),
            patch("pynchy.host.container_manager.orchestrator._ensure_agent_image"),
            patch(
                "pynchy.host.container_manager.security.gate.resolve_security",
                return_value=MagicMock(),
            ),
            patch("pynchy.host.container_manager.security.gate.create_gate"),
            patch(
                "pynchy.host.git_ops.repo.get_repo_context",
                side_effect=selected_repo_context,
            ) as get_repo_context,
            patch(
                "pynchy.host.git_ops.repo.resolve_repos_for_group",
                side_effect=PermissionError("unselected repository returned 403"),
            ) as resolve_workspace_repos,
            patch(
                "pynchy.host.git_ops.worktree.ensure_worktree",
                return_value=WorktreeResult(path=worktree_path),
            ) as ensure_worktree,
            patch(
                "pynchy.host.container_manager.mcp.manager.get_mcp_manager",
                return_value=None,
            ),
            patch(
                "pynchy.host.container_manager.orchestrator._container_cli",
                runtime.cli,
            ),
            patch(
                "pynchy.host.container_manager.orchestrator._ensure_agent_image",
                MagicMock(),
            ),
            patch(
                "pynchy.plugins.runtimes.api.get_runtime",
                return_value=runtime,
            ),
            patch("pynchy.host.container_manager.gateway.get_gateway", return_value=None),
            patch(
                "pynchy.host.orchestrator.agent_runner._await_query",
                new=AsyncMock(return_value="success"),
            ),
            patch(
                "pynchy.host.orchestrator.agent_runner.clear_session",
                new_callable=AsyncMock,
            ),
        ):
            result = await run_agent(
                deps,
                TEST_GROUP,
                "chat",
                [{"content": "review pynchy"}],
                is_scheduled_task=True,
                repo_access_override=selected_slug,
            )

        assert result == "success"
        spawn = deps.container_agent_operations.start_session
        spawn.assert_awaited_once()
        assert spawn.await_args.args[1].repo_access == selected_slug
        get_repo_context.assert_not_called()
        resolve_workspace_repos.assert_not_called()
        ensure_worktree.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("session_id", "should_reset"),
        [
            ("codex:019f47ec-2cc1-7920-84e7-64e85277a1ad", True),
            ("codex:gpt-5.5:019f47ec-2cc1-7920-84e7-64e85277a1ad", False),
            ("codex:gpt-5.6-sol:019f47ec-2cc1-7920-84e7-64e85277a1ad", True),
            ("claude-session-1", False),
        ],
    )
    async def test_run_agent_resets_only_incompatible_codex_sessions(
        self, session_id: str, should_reset: bool
    ):
        deps = _AgentRunnerDeps({TEST_GROUP.folder: session_id})
        ctx = self._ctx(session_id)
        settings = make_settings(agent=AgentConfig(model="gpt-5.5"))

        with (
            patch.object(deps, "agent_execution_runtime", _agent_runtime(settings)),
            patch(
                "pynchy.host.orchestrator.agent_runner.pre_container_setup",
                new_callable=AsyncMock,
                return_value=ctx,
            ),
            patch.object(deps.container_agent_operations, "get_session", return_value=None),
            patch(
                "pynchy.host.orchestrator.agent_runner._cold_start",
                new_callable=AsyncMock,
                return_value="success",
            ) as cold_start,
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
            result = await run_agent(deps, TEST_GROUP, "chat", [])

        assert result == "success"
        cold_ctx = cold_start.await_args.args[4]
        expected_session_id = None if should_reset else session_id
        assert cold_ctx.session_id == expected_session_id
        assert (TEST_GROUP.folder in deps.sessions) is not should_reset
        assert destroy_session.await_count == int(should_reset)
        assert clear_session.await_count == int(should_reset)

    @pytest.mark.asyncio
    async def test_scheduled_memory_opt_out_replaces_a_warm_mounted_worker(self):
        deps = _AgentRunnerDeps({"test-group": "session-1"})
        ctx = self._ctx("session-1")
        session = MagicMock(is_alive=True)

        with (
            patch(
                "pynchy.host.orchestrator.agent_runner.pre_container_setup",
                new_callable=AsyncMock,
                return_value=ctx,
            ),
            patch.object(
                deps.container_agent_operations,
                "get_session",
                return_value=session,
            ),
            patch.object(
                deps.container_agent_operations,
                "destroy_session",
                new_callable=AsyncMock,
            ) as destroy_session,
            patch(
                "pynchy.host.orchestrator.agent_runner._cold_start",
                new_callable=AsyncMock,
                return_value="success",
            ) as cold_start,
        ):
            result = await run_agent(
                deps,
                TEST_GROUP,
                "chat",
                [],
                is_scheduled_task=True,
                automation_memory_dir=None,
            )

        assert result == "success"
        destroy_session.assert_awaited_once_with(TEST_GROUP.folder)
        cold_start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_agent_dispatches_host_execution_mode_to_host_runner(self, tmp_path: Path):
        group = WorkspaceProfile(
            jid="host@g.us",
            name="Host Group",
            folder="host-group",
            trigger="@pynchy",
            is_admin=True,
        )
        deps = _AgentRunnerDeps({"host-group": "session-0"})
        ctx = self._ctx("session-0")
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
        deps.host_runtime_operations.build_agent_environment = MagicMock(
            return_value={"OPENAI_BASE_URL": "http://192.168.64.1:4000"}
        )
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
            patch(
                "pynchy.host.orchestrator.agent_runner._cold_start",
                new_callable=AsyncMock,
                return_value="container-called",
            ) as cold_start,
        ):
            result = await run_agent(deps, group, "chat", [{"content": "hi"}])

        assert result == "success"
        cold_start.assert_not_awaited()
        run_host_input.assert_awaited_once()
        input_data = run_host_input.await_args.args[0]
        assert input_data.group_folder == "host-group"
        assert input_data.session_id == "session-0"
        assert run_host_input.await_args.kwargs["cwd"] == tmp_path
        assert run_host_input.await_args.kwargs["env"]["OPENAI_BASE_URL"] == (
            "http://localhost:4000"
        )
        on_process_started = run_host_input.await_args.kwargs["on_process_started"]
        host_process = MagicMock(spec=asyncio.subprocess.Process)
        on_process_started(host_process)
        deps.queue.acquire_host_process.assert_called_once_with(RuntimeTarget.from_workspace(group))
        deps.queue.register_host_process.assert_called_once_with(
            deps.queue.acquire_host_process.return_value,
            host_process,
            "host-agent-runner",
            input_data.invocation_ts,
        )

    @pytest.mark.asyncio
    async def test_host_worktree_preparation_failure_does_not_start_host_process(self):
        group = WorkspaceProfile(
            jid="routed@g.us",
            name="Routed Host Group",
            folder="host__thread_conversation-conv_failed",
            trigger="@pynchy",
            is_admin=True,
        )
        deps = _AgentRunnerDeps()
        deps.broadcast_host_message = AsyncMock()
        ctx = self._ctx("legacy-session")
        ctx.is_admin = True

        with (
            patch(
                "pynchy.host.orchestrator.agent_runner.pre_container_setup",
                new_callable=AsyncMock,
                return_value=ctx,
            ),
            patch(
                "pynchy.host.orchestrator.agent_runner.agent_core_config",
                return_value=None,
            ),
            patch(
                "pynchy.host.orchestrator.agent_runner._session_model_mismatch",
                return_value=True,
            ),
            patch(
                "pynchy.host.orchestrator.agent_runner.clear_session",
                new_callable=AsyncMock,
            ),
            patch(
                "pynchy.host.orchestrator.agent_runner._host_execution_cwd",
                side_effect=host_execution.HostExecutionCwdError("legacy parent is ahead of main"),
            ) as host_execution_cwd,
            patch(
                "pynchy.host.orchestrator.agent_runner._run_host_execution",
                new_callable=AsyncMock,
            ) as run_host_execution,
        ):
            result = await run_agent(deps, group, "chat", [{"content": "resume"}])

        assert result == "error"
        deps.broadcast_host_message.assert_awaited_once_with(
            "chat",
            "Host execution blocked: legacy parent is ahead of main",
        )
        assert host_execution_cwd.call_args.kwargs["recovered"] is True
        run_host_execution.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_host_execution_clears_active_routed_repository_after_failure(
        self, tmp_path: Path
    ):
        group = WorkspaceProfile(
            jid="routed@g.us",
            name="Routed Host Group",
            folder="host__thread_conversation-conv_active",
            trigger="@pynchy",
            is_admin=True,
        )
        repo_access = "owner/scheduled-override"
        deps = _AgentRunnerDeps()
        ctx = self._ctx()
        ctx.repo_access = repo_access
        ctx.repo_accesses = [repo_access]

        def run_host_execution(*_args: object, **_kwargs: object) -> str:
            route = host_execution.active_routed_host_repo(group.folder)
            assert route is not None
            assert route.repo_access == repo_access
            assert route.turn_id == ctx.turn_id
            raise RuntimeError("host runner failed")

        with (
            patch(
                "pynchy.host.orchestrator.agent_runner.pre_container_setup",
                new_callable=AsyncMock,
                return_value=ctx,
            ) as pre_container_setup,
            patch(
                "pynchy.host.orchestrator.agent_runner._host_execution_cwd",
                return_value=host_execution.HostExecutionCwd(tmp_path, repo_access=repo_access),
            ) as host_execution_cwd,
            patch(
                "pynchy.host.orchestrator.agent_runner._run_host_execution",
                side_effect=run_host_execution,
            ),
            pytest.raises(RuntimeError, match="host runner failed"),
        ):
            await run_agent(
                deps,
                group,
                "chat",
                [{"content": "run"}],
                is_scheduled_task=True,
                repo_access_override=repo_access,
            )

        assert pre_container_setup.await_args.args[0].repo_access_override == repo_access
        assert host_execution_cwd.call_args.kwargs["repo_accesses"] == [repo_access]
        assert host_execution.active_routed_host_repo(group.folder) is None
