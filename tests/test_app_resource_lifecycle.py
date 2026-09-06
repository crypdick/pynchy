"""Public resource-lifecycle behavior for the application composition root."""

from __future__ import annotations

# allow: file-length -- application lifecycle scenarios share one composition fixture.
import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

import pynchy.host.orchestrator.app as app_module
from pynchy.config.api import (
    LearningConfig,
    McpTool,
    McpToolConfig,
    ObsidianLearningConfig,
    ProfileConfig,
    WorkspaceConfig,
    WorkspaceThreadConfig,
)
from pynchy.conversation.models import Conversation, ConversationSubject
from pynchy.conversation_primitives import (
    ConversationId,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
)
from pynchy.host.orchestrator.api import resolve_workspace_placement
from pynchy.host.orchestrator.app import PynchyApp
from pynchy.identifiers import GroupFolder, SessionId
from pynchy.learning_packets import LearningPacket
from pynchy.plugins.contracts import NewMessage
from pynchy.scheduling.types import ScheduledTask, SessionPolicy
from pynchy.turn_outcomes import TurnOutcome
from pynchy.work_items.api import WorkItemExecutionStatus
from pynchy.workspace.api import WorkspaceProfile

if TYPE_CHECKING:
    from pathlib import Path

from conftest import make_settings


class _CloseableObserver:
    name = "test-observer"

    def __init__(self) -> None:
        self.closed = False

    def subscribe(self, _event_bus: object) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class _HttpRunner:
    def __init__(self) -> None:
        self.cleaned = False

    async def cleanup(self) -> None:
        self.cleaned = True


def test_application_composes_learning_and_missing_workspace_profiles(tmp_path) -> None:
    settings = app_module.get_settings()
    original = (settings.learning, settings.profiles, settings.workspaces)
    settings.learning = LearningConfig(
        enabled=True,
        obsidian=ObsidianLearningConfig(vault_root=str(tmp_path / "vault")),
    )
    settings.profiles = {"default": ProfileConfig()}
    settings.workspaces = {
        "root": WorkspaceConfig(
            profiles=["default"],
            threads=[
                WorkspaceThreadConfig(
                    name="child",
                    workspace="child",
                    profiles=["default"],
                )
            ],
        )
    }
    try:
        app = PynchyApp()
        assert app.host_runtime_operations.host_learning_vault("child") == (tmp_path / "vault")

        root = WorkspaceProfile(jid="root", name="Root", folder="root", trigger="@Pynchy")
        placement = resolve_workspace_placement([root], "child")
        assert placement is not None
        assert placement.owner.folder == "child"
        assert placement.control_parent is root
    finally:
        settings.learning, settings.profiles, settings.workspaces = original


def _learning_packet() -> LearningPacket:
    return LearningPacket(
        job_id="job-1",
        chat_jid="chat",
        group_folder="chat",
        profile="default",
        created_at="2026-07-31T00:00:00Z",
        messages=[],
        final_answer=None,
        tool_counts={},
        error_snippets=[],
        loaded_skills=[],
        provenance={},
    )


async def test_application_owns_attached_resources_through_shutdown() -> None:
    app = PynchyApp()
    observer = _CloseableObserver()
    runner = _HttpRunner()

    app.attach_observers([observer])
    app.set_http_runner(runner)

    await app.close_observers()
    await app.cleanup_http_runner()
    await app.cleanup_http_runner()

    assert observer.closed is True
    assert runner.cleaned is True


async def test_live_routed_session_binding_updates_memory_and_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PynchyApp()
    persist_session = AsyncMock()
    monkeypatch.setattr(app_module, "set_session", persist_session)

    await app.bind_routed_session("project", SessionId("active-session"))

    assert app.sessions == {"project": SessionId("active-session")}
    persist_session.assert_awaited_once_with(GroupFolder("project"), SessionId("active-session"))


def test_application_shutdown_transition_is_idempotent() -> None:
    app = PynchyApp()

    assert app.is_shutting_down() is False
    assert app.begin_shutdown() is True
    assert app.is_shutting_down() is True
    assert app.begin_shutdown() is False


def test_application_dispatch_cursor_preserves_the_furthest_in_flight_message() -> None:
    app = PynchyApp()
    app.last_agent_timestamp["chat"] = "2026-07-28T10:00:00Z"

    app.mark_dispatched("chat", "2026-07-28T10:00:01Z")
    app.mark_dispatched("chat", "2026-07-28T10:00:00Z")

    assert app.routing_cursor("chat") == "2026-07-28T10:00:01Z"
    assert app.pop_dispatched("chat", "fallback") == "2026-07-28T10:00:01Z"
    assert app.routing_cursor("chat") == "2026-07-28T10:00:00Z"


def test_application_dispatch_sequence_cursor_uses_numeric_order() -> None:
    app = PynchyApp()

    app.mark_dispatched("chat", "sequence:10")
    app.mark_dispatched("chat", "sequence:9")

    assert app.routing_cursor("chat") == "sequence:10"


def test_application_exposes_update_offer_git_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = PynchyApp()
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        app_module,
        "get_local_head_sha",
        lambda root: calls.append(("head", root)) or "local-sha",
    )
    monkeypatch.setattr(app_module, "get_deploy_config_hash", lambda: "config-hash")
    monkeypatch.setattr(app_module, "get_head_sha", lambda: "current-sha")
    monkeypatch.setattr(
        app_module,
        "host_update_main",
        lambda root: calls.append(("update", root)) or True,
    )
    monkeypatch.setattr(app_module, "needs_deploy", lambda old, new: old != new)
    monkeypatch.setattr(app_module, "needs_container_rebuild", lambda old, new: old == new)

    assert app.get_local_head_sha(tmp_path) == "local-sha"
    assert app.get_deploy_config_hash() == "config-hash"
    assert app.current_deploy_revision() == ("current-sha", "config-hash")
    assert app.host_update_main(tmp_path) is True
    assert app.needs_deploy("old", "new") is True
    assert app.needs_container_rebuild("same", "same") is True
    assert calls == [("head", tmp_path), ("update", tmp_path)]


def test_application_refreshes_personalization_skills_through_host_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PynchyApp()
    refreshed: list[str] = []
    monkeypatch.setattr(
        app_module,
        "refresh_personalized_agent_skills",
        refreshed.append,
    )

    app.refresh_personalized_agent_skills("group")

    assert refreshed == ["group"]


def test_application_syncs_personalization_with_the_validator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = PynchyApp()
    validator = object()
    calls: list[tuple[Path, object]] = []

    def sync(root: Path, validate: object) -> str:
        calls.append((root, validate))
        return "synced"

    monkeypatch.setattr("pynchy.config.api.validate_personalization_configuration", validator)
    monkeypatch.setattr("pynchy.host.git_ops.api.sync_personalization_repo", sync)

    assert app.sync_personalization(tmp_path) == "synced"
    assert calls == [(tmp_path, validator)]


async def test_application_routes_manual_redeploy_and_temporal_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PynchyApp()
    redeploy = AsyncMock()
    channel_reconciliation = AsyncMock()
    linear_reconciliation = AsyncMock()
    monkeypatch.setattr(app_module.session_handler, "trigger_manual_redeploy", redeploy)
    monkeypatch.setattr(
        app_module.temporal_scheduler,
        "start_channel_reconciliation_workflow",
        channel_reconciliation,
    )
    monkeypatch.setattr(
        app_module.temporal_scheduler,
        "start_linear_work_item_reconciliation_workflow",
        linear_reconciliation,
    )

    await app.trigger_manual_redeploy("chat")
    await app.start_channel_reconciliation()
    await app.start_linear_work_item_reconciliation()

    redeploy.assert_awaited_once_with(app, "chat", source_message=None)
    channel_reconciliation.assert_awaited_once_with()
    linear_reconciliation.assert_awaited_once_with()


async def test_application_routes_message_and_reaction_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PynchyApp()
    process = AsyncMock(return_value=TurnOutcome.COMPLETED)
    interactive = AsyncMock()
    interrupted = AsyncMock()
    reaction = AsyncMock()
    clear_confirmation = AsyncMock()
    monkeypatch.setattr(app_module.message_handler, "run_queued_message_turn", process)
    monkeypatch.setattr(
        app_module.temporal_scheduler,
        "start_interactive_message_workflow",
        interactive,
    )
    monkeypatch.setattr(
        app_module.temporal_scheduler,
        "start_interrupted_turn_workflow",
        interrupted,
    )
    monkeypatch.setattr(app_module.channel_handler, "send_reaction_to_outbound", reaction)
    monkeypatch.setattr(app_module.session_handler, "send_clear_confirmation", clear_confirmation)

    assert await app.process_group_messages("chat") is TurnOutcome.COMPLETED
    await app.start_interactive_turn("chat")
    await app.start_interrupted_turn("turn", "group")
    await app.send_reaction_to_outbound("chat", {"discord": "message"}, "thumbsup")
    await app.send_clear_confirmation("chat")

    process.assert_awaited_once_with(app, "chat")
    interactive.assert_awaited_once_with("chat")
    interrupted.assert_awaited_once_with("turn", "group")
    reaction.assert_awaited_once_with(app, "chat", {"discord": "message"}, "thumbsup")
    clear_confirmation.assert_awaited_once_with(app, "chat")


async def test_application_routes_inbound_messages_and_interaction_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PynchyApp()
    message = NewMessage(
        id="message-1",
        chat_jid="chat",
        sender="user",
        sender_name="User",
        content="hello",
        timestamp="2026-07-31T00:00:00Z",
    )
    ingest = AsyncMock()
    inbound = AsyncMock()
    reaction = AsyncMock()
    update_offer = AsyncMock(return_value=True)
    monkeypatch.setattr(app_module.session_handler, "ingest_user_message", ingest)
    monkeypatch.setattr(app_module.session_handler, "on_inbound", inbound)
    monkeypatch.setattr(app_module.reaction_handler, "handle_reaction", reaction)
    monkeypatch.setattr(app_module.update_offer, "handle_update_offer_answer", update_offer)

    await app.ingest_user_message(message, source_channel="slack")
    await app.on_inbound("chat", message)
    await app.on_reaction("chat", "message-1", "user", "thumbsup")
    await app.on_ask_user_answer("request-1", {"answer": "yes"})

    ingest.assert_any_await(app, message, source_channel="slack")
    inbound.assert_awaited_once_with(app, "chat", message)
    reaction.assert_awaited_once_with(app, "chat", "message-1", "user", "thumbsup")
    update_offer.assert_awaited_once_with("request-1", {"answer": "yes"}, app)


async def test_application_routes_non_update_offer_answers_to_ask_user_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PynchyApp()
    update_offer = AsyncMock(return_value=False)
    ask_user = AsyncMock()
    monkeypatch.setattr(app_module.update_offer, "handle_update_offer_answer", update_offer)
    monkeypatch.setattr(app_module.ask_user_handler, "handle_ask_user_answer", ask_user)

    await app.on_ask_user_answer("request-1", {"answer": "yes"})

    update_offer.assert_awaited_once_with("request-1", {"answer": "yes"}, app)
    ask_user.assert_awaited_once_with("request-1", {"answer": "yes"}, app)


def test_application_delegates_filtering_and_idle_callback_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PynchyApp()
    group = WorkspaceProfile(jid="chat", name="Chat", folder="chat", trigger="@Pynchy")

    def callback() -> None:
        return None

    class _Session:
        callback: object | None = None

        def set_idle_callback(self, value: object) -> None:
            self.callback = value

    calls: list[tuple[object, ...]] = []
    session = _Session()

    def filter_messages(*args: object) -> list[object]:
        calls.append(args)
        return []

    monkeypatch.setattr(app_module.access, "filter_allowed_messages", filter_messages)
    monkeypatch.setattr(app_module, "get_session", lambda _: session)

    assert app.filter_allowed_messages([], group, None) == []
    app.register_idle_callback(GroupFolder("chat"), callback)

    assert calls == [([], group, None)]
    assert session.callback is callback


def test_application_does_not_register_idle_callback_without_live_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PynchyApp()
    callback = MagicMock()
    monkeypatch.setattr(app_module, "get_session", lambda _group: None)

    app.register_idle_callback(GroupFolder("chat"), callback)

    callback.assert_not_called()


def test_application_delegates_host_and_workspace_policy_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PynchyApp()
    group = WorkspaceProfile(jid="chat", name="Chat", folder="chat", trigger="@Pynchy")
    monkeypatch.setattr(app_module, "is_repo_dirty", lambda: True)
    monkeypatch.setattr(app_module, "has_api_credentials", lambda: False)
    monkeypatch.setattr(app_module, "linear_workspace_enabled", lambda _: False)
    monkeypatch.setattr(app.queue, "has_active_host_process", lambda folder: folder == "chat")

    assert app.repo_is_dirty() is True
    assert app.has_api_credentials() is False
    assert app.has_active_host_process("chat") is True
    assert app.linear_workspace_enabled(group) is False


async def test_application_defers_channel_catch_up_when_temporal_startup_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PynchyApp()

    def temporal_active() -> bool:
        return True

    monkeypatch.setattr(
        app_module.temporal_scheduler, "temporal_scheduler_runtime_active", temporal_active
    )
    monkeypatch.setattr(
        app,
        "start_channel_reconciliation",
        AsyncMock(
            side_effect=app_module.temporal_scheduler.TemporalRuntimeUnavailableError("late")
        ),
    )

    await app.catch_up_channels()


def test_application_exposes_selected_workspace_environment_through_host_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(
        chrome_profiles=["work"],
        tools={
            "browser.work": McpTool(
                type="mcp",
                mcp=McpToolConfig(runtime="url", url="https://example.test/mcp"),
            ),
            "browser.other": McpTool(
                type="mcp",
                mcp=McpToolConfig(runtime="url", url="https://other.example.test/mcp"),
            ),
        },
    )
    access = MagicMock(workspace_env={"SELECTED": "yes"})
    resolved = MagicMock(tools=("browser.work", "browser.other", "missing", "not-mcp"))
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(app_module, "load_resolved_tool_access", lambda _: access)
    monkeypatch.setattr(app_module, "load_resolved_config", lambda *_args, **_kwargs: resolved)

    app = PynchyApp()
    environment = app.host_runtime_operations.build_agent_environment(
        is_admin=False,
        group_folder="group",
        extra_env_vars={},
    )

    assert environment["SELECTED"] == "yes"
    assert environment["PYNCHY_CHROME_PROFILES"] == "work"


async def test_application_runs_learning_review_through_the_workspace_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PynchyApp()
    group = WorkspaceProfile(jid="chat", name="Chat", folder="chat", trigger="@Pynchy")
    packet = _learning_packet()
    app.run_agent = AsyncMock(return_value="success")

    async def review(_packet, run_agent_via_queue, _prompt):
        assert await run_agent_via_queue(group, "chat", []) == "success"
        return "completed"

    monkeypatch.setattr(app_module, "run_host_learning_review", review)
    monkeypatch.setattr(app_module, "_read_current_prompt", lambda _: "review prompt")
    assert await app.run_learning_review(packet) == "completed"
    app.run_agent.assert_awaited_once()
    assert app.queue.snapshot()["_meta"]["active_count"] == 0
    await app.queue.shutdown()

    start_review = AsyncMock()
    monkeypatch.setattr(
        app_module.temporal_scheduler, "start_learning_review_workflow", start_review
    )
    await app.start_learning_review_workflow(packet)
    start_review.assert_awaited_once_with(packet)


async def test_learning_review_surfaces_queue_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    app = PynchyApp()
    group = WorkspaceProfile(jid="chat", name="Chat", folder="chat", trigger="@Pynchy")
    await app.queue.shutdown()

    async def review(_packet, run_agent_via_queue, _prompt):
        return await run_agent_via_queue(group, "chat", [])

    monkeypatch.setattr(app_module, "run_host_learning_review", review)
    monkeypatch.setattr(app_module, "_read_current_prompt", lambda _: "review prompt")
    with pytest.raises(RuntimeError, match="Thread queue rejected scheduled task"):
        await app.run_learning_review(_learning_packet())


@pytest.mark.parametrize("error", [RuntimeError("agent failed"), asyncio.CancelledError()])
async def test_learning_review_propagates_agent_failure_or_cancellation(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    app = PynchyApp()
    group = WorkspaceProfile(jid="chat", name="Chat", folder="chat", trigger="@Pynchy")
    app.run_agent = AsyncMock(side_effect=error)

    async def review(_packet, run_agent_via_queue, _prompt):
        return await run_agent_via_queue(group, "chat", [])

    monkeypatch.setattr(app_module, "run_host_learning_review", review)
    monkeypatch.setattr(app_module, "_read_current_prompt", lambda _: "review prompt")
    with pytest.raises(type(error)) as raised:
        await app.run_learning_review(_learning_packet())
    assert raised.value is error
    app.run_agent.assert_awaited_once()
    assert app.queue.snapshot()["_meta"]["active_count"] == 0
    await app.queue.shutdown()


async def test_cancelling_learning_review_stops_its_active_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PynchyApp()
    group = WorkspaceProfile(jid="chat", name="Chat", folder="chat", trigger="@Pynchy")
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def run_agent(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def review(_packet, run_agent_via_queue, _prompt):
        return await run_agent_via_queue(group, "chat", [])

    app.run_agent = run_agent
    monkeypatch.setattr(app_module, "run_host_learning_review", review)
    monkeypatch.setattr(app_module, "_read_current_prompt", lambda _: "review prompt")
    owner = asyncio.create_task(app.run_learning_review(_learning_packet()))
    await started.wait()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    await stopped.wait()
    assert app.queue.snapshot()["_meta"]["active_count"] == 0
    await app.queue.shutdown()


async def test_application_runs_declared_canaries_through_the_canary_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PynchyApp()
    run_canaries = AsyncMock(return_value=[])
    monkeypatch.setattr(app_module, "run_declared_canaries", run_canaries)

    assert await app.run_declared_canaries("default", ("scenario",)) == []

    run_canaries.assert_awaited_once_with(
        target_profile="default",
        scenario_ids=("scenario",),
        scheduler_deps=app,
    )


async def test_application_skips_linear_reconciliation_without_configured_boards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PynchyApp()
    monkeypatch.setattr(app_module, "linear_workspace_boards", dict)

    assert await app.reconcile_linear_work_items() is None


async def test_application_reports_admitted_linear_work_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PynchyApp()
    boards = {"project": object()}
    reconcile = AsyncMock(return_value=[object(), object()])
    monkeypatch.setattr(app_module, "linear_workspace_boards", lambda: boards)
    monkeypatch.setattr(app_module, "reconcile_all_linear_work_items", reconcile)

    assert await app.reconcile_linear_work_items() == 2
    reconcile.assert_awaited_once_with(
        app.workspaces,
        boards,
        review_plan=app.review_linear_plan,
        broadcast_host_message=app.broadcast_host_message,
        defer_plan_review=app_module.temporal_scheduler.start_linear_plan_review_workflow,
    )


async def test_application_adapts_scheduled_execution_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PynchyApp()
    execution = MagicMock(id="execution-1", status=WorkItemExecutionStatus.COMPLETED)
    get_execution = AsyncMock(side_effect=[execution, None])
    monkeypatch.setattr(app_module, "get_work_item_execution_for_task", get_execution)

    lifecycle = await app.scheduled_execution_lifecycle("task-1")

    assert lifecycle is not None
    assert lifecycle.execution_id == "execution-1"
    assert lifecycle.status == "completed"
    assert lifecycle.has_explicit_outcome is True
    assert await app.scheduled_execution_lifecycle("task-2") is None
    assert get_execution.await_args_list[0].args == ("task-1",)
    assert get_execution.await_args_list[1].args == ("task-2",)


async def test_application_adapts_scheduled_task_storage_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PynchyApp()
    conversation = Conversation(
        id=ConversationId("conversation-1"),
        workspace=GroupFolder("chat"),
        subject=ConversationSubject(
            namespace=ConversationSubjectNamespace("test"),
            key=ConversationSubjectKey("conversation-1"),
        ),
        session_id=None,
        created_at="2026-07-31T00:00:00Z",
        updated_at="2026-07-31T00:00:00Z",
    )
    get_conversation = AsyncMock(return_value=conversation)
    update_task = AsyncMock()
    cancel_task = AsyncMock()
    monkeypatch.setattr(app_module, "get_conversation", get_conversation)
    monkeypatch.setattr(app_module, "update_task", update_task)
    monkeypatch.setattr(app_module, "cancel_task_and_checkpoint", cancel_task)

    assert await app.get_scheduled_conversation("conversation-1") is conversation
    await app.persist_scheduled_task_updates("task-1", {"status": "paused"})
    await app.cancel_scheduled_task("task-2")

    get_conversation.assert_awaited_once_with("conversation-1")
    update_task.assert_awaited_once_with("task-1", {"status": "paused"})
    cancel_task.assert_awaited_once_with("task-2")


async def test_application_forwards_lifecycle_adapter_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PynchyApp()
    group = WorkspaceProfile(jid="chat", name="Chat", folder="chat", trigger="@Pynchy")
    task = ScheduledTask(
        id="task-1",
        group_folder="chat",
        chat_jid="chat",
        prompt="run",
        schedule_type="once",
        schedule_value="2026-07-31T00:00:00Z",
        session_policy=SessionPolicy.CONTINUE,
    )
    context_reset = AsyncMock()
    plugin_reset = AsyncMock()
    scheduled_reset = AsyncMock()
    end_session = AsyncMock()
    rebind_workspace = AsyncMock()
    create_todo = AsyncMock(return_value={"id": "todo-1"})
    monkeypatch.setattr(app_module.session_handler, "handle_context_reset", context_reset)
    monkeypatch.setattr(app_module, "prepare_context_reset", plugin_reset)
    monkeypatch.setattr(
        app_module.session_handler, "handle_scheduled_context_reset", scheduled_reset
    )
    monkeypatch.setattr(app_module.session_handler, "handle_end_session", end_session)
    monkeypatch.setattr(app_module, "rebind_workspace_runtime", rebind_workspace)
    monkeypatch.setattr(app_module, "create_linear_workspace_todo", create_todo)

    await app.handle_context_reset("chat", group, "timestamp")
    await app.prepare_context_reset(group)
    await app.reset_scheduled_context(task, group, "occurrence-1")
    await app.handle_end_session("chat", group, "timestamp")
    await app.rebind_workspace(group)
    assert await app.create_linear_workspace_todo(group, "Title") == {"id": "todo-1"}

    context_reset.assert_awaited_once_with(app, "chat", group, "timestamp", source_message=None)
    plugin_reset.assert_awaited_once_with(app.plugin_manager, group)
    scheduled_reset.assert_awaited_once_with(app, "task-1", group, "occurrence-1")
    end_session.assert_awaited_once_with(app, "chat", group, "timestamp", source_message=None)
    rebind_workspace.assert_awaited_once_with(group, app.workspaces, app.queue)
    create_todo.assert_awaited_once_with(group, "Title")


async def test_application_unregisters_workspace_from_memory_and_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PynchyApp()
    app.workspaces["chat"] = WorkspaceProfile(
        jid="chat", name="Chat", folder="chat", trigger="@Pynchy"
    )
    delete_workspace = AsyncMock()
    monkeypatch.setattr(app_module, "delete_workspace_profile", delete_workspace)

    await app.unregister_workspace("chat")

    assert "chat" not in app.workspaces
    delete_workspace.assert_awaited_once_with("chat")


async def test_application_forwards_ask_user_answers_as_system_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PynchyApp()
    store = AsyncMock()
    broadcast = AsyncMock()
    start_turn = AsyncMock()
    monkeypatch.setattr(app_module, "store_message", store)
    monkeypatch.setattr(app, "broadcast_host_message", broadcast)
    monkeypatch.setattr(app, "start_interactive_turn", start_turn)

    await app.enqueue_message("chat", "answer")

    message = store.await_args.args[0]
    assert message.chat_jid == "chat"
    assert message.sender == "system"
    assert message.content == "answer"
    assert message.message_type == "system"
    store.assert_awaited_once_with(message, message_type="system")
    broadcast.assert_awaited_once_with("chat", "😎 Answer forwarded to agent")
    start_turn.assert_awaited_once_with("chat")
