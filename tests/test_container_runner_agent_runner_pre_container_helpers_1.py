"""Tests for the container runner."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.agent_protocol.api import (
    ContainerOutput,
)
from pynchy.conversation.models import (
    ControlSurface,
    Conversation,
    ConversationControlBinding,
    ConversationId,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
)
from pynchy.host.orchestrator.agent_runner import (
    PreContainerSetupRequest,
    append_post_work_prompt,
    build_admin_system_notices,
    pre_container_setup,
    session_tracking_output_handler,
)
from pynchy.host.orchestrator.conversation_control import ConversationControlClosedError
from pynchy.identifiers import (
    ChatJid,
    GroupFolder,
    SessionId,
)
from pynchy.state.api import SessionSecurityTaint
from pynchy.workspace.api import (
    WorkspaceProfile,
)
from tests.container_runner_support import (
    _AgentRunnerDeps,
)

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


def test_append_post_work_prompt_preserves_input_and_appends_to_last_message():
    messages = [{"content": "Run objective", "metadata": {"source": "task"}}]

    result = append_post_work_prompt(messages, "[POST-WORK REFLECTION]")

    assert result == [
        {
            "content": "Run objective\n\n[POST-WORK REFLECTION]",
            "metadata": {"source": "task"},
        }
    ]
    assert messages == [{"content": "Run objective", "metadata": {"source": "task"}}]


def test_append_post_work_prompt_preserves_non_text_content():
    messages = [{"content": ["structured", "content"]}]

    assert append_post_work_prompt(messages, "[POST-WORK REFLECTION]") is messages


_CR_CREDS = "pynchy.host.container_manager.credentials"
_CR_ORCH = "pynchy.host.container_manager.orchestrator"
_GATEWAY = "pynchy.host.container_manager.gateway"


_SETTINGS_MODULES = [
    "pynchy.host.orchestrator.workspace_config",
]

_test_settings: ContextVar[Any | None] = ContextVar("test_settings", default=None)


class TestAgentRunnerPreContainerHelpers:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "output",
        [
            ContainerOutput(status="error", type="result", query_id="query-1"),
            ContainerOutput(
                status="success",
                type="tool_result",
                query_id="query-1",
                tool_result_id="tool-1",
                tool_result_is_error=True,
            ),
        ],
    )
    async def test_session_tracking_output_handler_logs_failures(
        self,
        output: ContainerOutput,
    ) -> None:
        deps = _AgentRunnerDeps()

        with patch("pynchy.host.orchestrator._agent_runner_preflight.logger.error") as log_error:
            handler = session_tracking_output_handler(
                deps,
                "test-group",
                "test@g.us",
                None,
            )
            await handler(output)

        log_error.assert_called_once_with(
            "Agent reported error output",
            group="test-group",
            chat_jid="test@g.us",
            query_id="query-1",
            output_type=output.type,
            tool_result_id=output.tool_result_id,
        )

    @pytest.mark.asyncio
    async def test_external_matrix_input_marks_security_taint(self):
        deps = _AgentRunnerDeps()
        taint = SessionSecurityTaint(corruption_tainted=True, secret_tainted=True)

        with (
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.mark_session_security_taint",
                new_callable=AsyncMock,
                return_value=taint,
            ) as mark_taint,
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.workspace_config.load_resolved_config",
                return_value=None,
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
                return_value=None,
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
                    group=TEST_GROUP,
                    chat_jid="test@g.us",
                    messages=[{"content": "external"}],
                    on_output=None,
                    extra_system_notices=None,
                    input_source="external:matrix",
                    is_scheduled_task=False,
                    repo_access_override=None,
                    runtime=deps.agent_execution_runtime,
                )
            )

        mark_taint.assert_awaited_once_with(
            GroupFolder("test-group"), corruption_tainted=True, secret_tainted=True
        )
        assert result.corruption_tainted is True
        assert result.secret_tainted is True
        assert result.post_work_prompt is None

    @pytest.mark.asyncio
    async def test_scheduled_preflight_resolves_post_work_prompt(self):
        deps = _AgentRunnerDeps()

        def read_prompts(names: list[str]) -> str:
            if names == ["executors/post-work-reflection"]:
                return "reflection prompt"
            return "executor prompt"

        with (
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.get_session_security_taint",
                new_callable=AsyncMock,
                return_value=SessionSecurityTaint(),
            ),
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.workspace_config.load_resolved_config",
                return_value=None,
            ),
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.workspace_config.prompt_ids_for_context",
                return_value=["executor/default"],
            ),
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.workspace_config.read_prompts",
                side_effect=read_prompts,
            ),
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.write_container_snapshots",
                new_callable=AsyncMock,
                return_value=1.0,
            ),
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.workspace_config.load_resolved_tool_access",
                return_value=None,
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
                    group=TEST_GROUP,
                    chat_jid="test@g.us",
                    messages=[{"content": "scheduled work"}],
                    on_output=None,
                    extra_system_notices=None,
                    input_source="scheduled_task",
                    is_scheduled_task=True,
                    repo_access_override=None,
                    runtime=deps.agent_execution_runtime,
                )
            )

        assert result.post_work_prompt == "reflection prompt"

    @pytest.mark.asyncio
    async def test_missing_executor_prompt_fails_before_broadcast(self):
        deps = _AgentRunnerDeps()

        with (
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.get_session_security_taint",
                new_callable=AsyncMock,
                return_value=SessionSecurityTaint(),
            ),
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.workspace_config.load_resolved_config",
                return_value=None,
            ),
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.workspace_config.prompt_ids_for_context",
                return_value=["soul/default", "executor/default"],
            ),
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.workspace_config.read_prompts",
                side_effect=["system prompt", None],
            ),
            pytest.raises(RuntimeError, match="Selected executor prompt"),
        ):
            await pre_container_setup(
                PreContainerSetupRequest(
                    deps=deps,
                    group=TEST_GROUP,
                    chat_jid="test@g.us",
                    messages=[{"content": "missing prompt"}],
                    on_output=None,
                    extra_system_notices=None,
                    input_source="user",
                    is_scheduled_task=False,
                    repo_access_override=None,
                    runtime=deps.agent_execution_runtime,
                )
            )

        assert deps.session_cleared == set()

    @pytest.mark.asyncio
    async def test_terminal_control_blocks_preflight_before_side_effects(self):
        deps = _AgentRunnerDeps()
        deps.session_cleared.add("project__thread_conversation-conv_terminal")
        conversation_id = ConversationId("conv_terminal")
        binding = ConversationControlBinding(
            conversation_id=conversation_id,
            surface=ControlSurface.DISCORD,
            parent_workspace=GroupFolder("project"),
            parent_jid=ChatJid("discord:channel:project"),
            thread_jid=ChatJid("discord:channel:terminal"),
            title="Terminal issue",
            updated_at="2026-07-27T00:00:00+00:00",
            closed=True,
        )
        conversation = Conversation(
            id=conversation_id,
            workspace=GroupFolder("project"),
            subject=ConversationSubject(
                namespace=ConversationSubjectNamespace("linear:org:issue"),
                key=ConversationSubjectKey("issue-terminal"),
            ),
            session_id=None,
            created_at="2026-07-27T00:00:00+00:00",
            updated_at="2026-07-27T00:00:00+00:00",
            control_closed=True,
        )
        taint = AsyncMock()

        with (
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.get_conversation_control_by_thread",
                new_callable=AsyncMock,
                return_value=binding,
            ),
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.get_conversation",
                new_callable=AsyncMock,
                return_value=conversation,
            ),
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.mark_session_security_taint",
                taint,
            ),
            pytest.raises(ConversationControlClosedError, match="conv_terminal"),
        ):
            await pre_container_setup(
                PreContainerSetupRequest(
                    deps=deps,
                    runtime=deps.agent_execution_runtime,
                    group=WorkspaceProfile(
                        jid="discord:channel:terminal",
                        name="Terminal issue",
                        folder="project__thread_conversation-conv_terminal",
                        trigger="@Pynchy",
                    ),
                    chat_jid="discord:channel:terminal",
                    messages=[{"content": "stale run"}],
                    on_output=None,
                    extra_system_notices=None,
                    input_source="external:linear",
                    is_scheduled_task=False,
                    repo_access_override=None,
                )
            )

        taint.assert_not_awaited()
        assert deps.session_cleared == {"project__thread_conversation-conv_terminal"}

    @pytest.mark.asyncio
    async def test_open_control_allows_preflight_to_continue(self):
        deps = _AgentRunnerDeps()
        binding = MagicMock(conversation_id=ConversationId("conv_open"))

        with (
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.get_conversation_control_by_thread",
                new_callable=AsyncMock,
                return_value=binding,
            ),
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.get_conversation",
                new_callable=AsyncMock,
                return_value=MagicMock(control_closed=False),
            ),
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.mark_session_security_taint",
                new_callable=AsyncMock,
                return_value=SessionSecurityTaint(),
            ),
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.get_session_security_taint",
                new_callable=AsyncMock,
                return_value=SessionSecurityTaint(),
            ),
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.workspace_config.load_resolved_config",
                return_value=None,
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
                return_value=None,
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
                    group=TEST_GROUP,
                    chat_jid="discord:channel:open",
                    messages=[{"content": "continue"}],
                    on_output=None,
                    extra_system_notices=None,
                    input_source="user",
                    is_scheduled_task=False,
                    repo_access_override=None,
                    runtime=deps.agent_execution_runtime,
                )
            )

        assert result.is_admin is False

    @pytest.mark.asyncio
    async def test_session_tracking_output_handler_records_session(self):
        deps = _AgentRunnerDeps()
        on_output = AsyncMock()
        conversation_id = ConversationId("conv_opaque-id-")
        binding = ConversationControlBinding(
            conversation_id=conversation_id,
            surface=ControlSurface.DISCORD,
            parent_workspace=GroupFolder("admin"),
            parent_jid=ChatJid("discord:channel:parent"),
            thread_jid=ChatJid("discord:channel:thread"),
            title="Opaque conversation",
            updated_at="2026-07-23T23:40:00+00:00",
        )
        output = ContainerOutput(
            status="success",
            type="system",
            system_subtype="thread.started",
            system_data={"session_id": "codex:thread-1"},
        )

        with (
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.set_session",
                new_callable=AsyncMock,
            ) as persist,
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.update_in_flight_session",
                new_callable=AsyncMock,
            ) as update_checkpoint,
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.get_conversation_control_by_thread",
                new_callable=AsyncMock,
                return_value=binding,
            ) as get_binding,
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.set_conversation_session",
                new_callable=AsyncMock,
            ) as persist_conversation,
        ):
            handler = session_tracking_output_handler(
                deps,
                "admin__thread_conversation-conv_opaque-id",
                "discord:channel:thread",
                on_output,
            )
            await handler(output)

        assert deps.sessions == {"admin__thread_conversation-conv_opaque-id": "codex:thread-1"}
        persist.assert_awaited_once()
        get_binding.assert_awaited_once_with(ChatJid("discord:channel:thread"))
        persist_conversation.assert_awaited_once_with(
            conversation_id,
            SessionId("codex:thread-1"),
        )
        update_checkpoint.assert_awaited_once_with(
            "admin__thread_conversation-conv_opaque-id",
            "codex:thread-1",
        )
        on_output.assert_awaited_once_with(output)

    @pytest.mark.asyncio
    async def test_session_tracking_output_handler_skips_unbound_chat(self):
        deps = _AgentRunnerDeps()
        output = ContainerOutput(
            status="success",
            type="system",
            system_subtype="thread.started",
            system_data={"session_id": "codex:thread-1"},
        )

        with (
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.set_session",
                new_callable=AsyncMock,
            ),
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.update_in_flight_session",
                new_callable=AsyncMock,
            ),
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.get_conversation_control_by_thread",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.set_conversation_session",
                new_callable=AsyncMock,
            ) as persist_conversation,
        ):
            handler = session_tracking_output_handler(
                deps,
                "admin",
                "discord:channel:admin",
                None,
            )
            await handler(output)

        persist_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_scheduled_session_tracks_durable_runtime_and_checkpoint(self):
        deps = _AgentRunnerDeps()
        conversation_id = ConversationId("conv_scheduled-thread")
        binding = ConversationControlBinding(
            conversation_id=conversation_id,
            surface=ControlSurface.DISCORD,
            parent_workspace=GroupFolder("project"),
            parent_jid=ChatJid("discord:channel:project"),
            thread_jid=ChatJid("discord:channel:linear-thread"),
            title="Scheduled issue",
            updated_at="2026-07-25T23:00:00+00:00",
        )
        output = ContainerOutput(
            status="success",
            type="system",
            system_subtype="thread.started",
            system_data={"session_id": "codex:scheduled-thread"},
        )

        with (
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.set_session",
                new_callable=AsyncMock,
            ) as persist,
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.update_in_flight_session",
                new_callable=AsyncMock,
            ) as update_checkpoint,
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.get_conversation_control_by_thread",
                new_callable=AsyncMock,
                return_value=binding,
            ) as get_binding,
            patch(
                "pynchy.host.orchestrator._agent_runner_preflight.set_conversation_session",
                new_callable=AsyncMock,
            ) as persist_conversation,
        ):
            handler = session_tracking_output_handler(
                deps,
                "project__thread_discord-channel-linear-thread",
                "discord:channel:linear-thread",
                None,
            )
            await handler(output)

        assert deps.sessions == {
            "project__thread_discord-channel-linear-thread": "codex:scheduled-thread"
        }
        persist.assert_awaited_once_with(
            GroupFolder("project__thread_discord-channel-linear-thread"),
            SessionId("codex:scheduled-thread"),
        )
        get_binding.assert_awaited_once_with(ChatJid("discord:channel:linear-thread"))
        persist_conversation.assert_awaited_once_with(
            conversation_id,
            SessionId("codex:scheduled-thread"),
        )
        update_checkpoint.assert_awaited_once_with(
            "project__thread_discord-channel-linear-thread",
            "codex:scheduled-thread",
        )

    def test_build_admin_system_notices_includes_repo_warnings_and_guidance(self):
        notices = build_admin_system_notices(
            is_admin=True,
            repo_dirty=True,
            unpushed_commits=2,
        )

        assert any("uncommitted local changes" in notice for notice in notices)
        assert any("haven't been pushed" in notice for notice in notices)
        assert notices[-1].startswith("Consider whether to address")

    @pytest.mark.parametrize(
        ("is_admin", "repo_dirty", "unpushed_commits"),
        [
            (False, True, 2),
            (True, False, 0),
            (True, True, 0),
            (True, False, 1),
        ],
    )
    def test_build_admin_system_notices_only_reports_relevant_repository_state(
        self,
        is_admin: bool,
        repo_dirty: bool,
        unpushed_commits: int,
    ) -> None:
        notices = build_admin_system_notices(
            is_admin=is_admin,
            repo_dirty=repo_dirty,
            unpushed_commits=unpushed_commits,
        )

        if not is_admin or (not repo_dirty and unpushed_commits == 0):
            assert notices == []
        else:
            assert notices[-1].startswith("Consider whether to address")
