"""Tests for message processing (message_handler) and routing (_message_routing).

Covers:
- intercept_special_command: reset, end session, redeploy, !commands
- process_group_messages: reset handoff, trigger filtering, cursor management,
  dirty repo check, error rollback, worktree merge
- _check_dirty_repo, advance_cursor, and reset handoff behavior
- start_message_loop: "btw" non-interrupting messages during active tasks
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from conftest import make_command_matcher, make_settings

from pynchy.agent_protocol.api import (
    ContainerOutput,
)
from pynchy.conversation.models import (
    ConversationClaimId,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalDeliveryReceipt,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.host.learning.api import capture as learning_capture
from pynchy.host.orchestrator.messaging.inbound import start_message_loop
from pynchy.host.orchestrator.messaging.pipeline import (
    MessageHandlerDeps,
    process_group_messages,
)
from pynchy.identifiers import (
    GroupFolder,
)
from pynchy.plugins.api import NewMessage
from pynchy.state import (
    admit_conversation_delivery,
    admit_external_delivery_receipt,
    claim_next_conversation_delivery,
    store_message,
)
from pynchy.workspace.api import (
    WorkspaceProfile,
)

# Commonly patched module paths — avoids repeating long strings and keeps
# line lengths under 100 chars.
_P_MSGS_SINCE = "pynchy.host.orchestrator.messaging.pipeline.get_messages_since"
_P_INTERCEPT = "pynchy.host.orchestrator.messaging.pipeline.intercept_special_command"
_P_FMT_SDK = "pynchy.host.orchestrator.messaging.formatter.format_messages_for_sdk"

# Patch paths for names imported in _message_routing (routing/loop tests).
_PR = "pynchy.host.orchestrator.messaging.inbound"
_PR_NEW_MSGS = f"{_PR}.get_new_messages"
_PR_MSGS_SINCE = f"{_PR}.get_messages_since"
_PR_INTERCEPT = f"{_PR}.intercept_special_command"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_deps(
    *,
    groups: dict | None = None,
    last_agent_ts: dict | None = None,
    last_timestamp: str = "",
) -> MagicMock:
    """Build a MessageHandlerDeps mock with sensible defaults."""
    deps = MagicMock(spec=MessageHandlerDeps)
    deps.command_matcher = make_command_matcher(make_settings())
    deps.workspaces = groups or {}
    deps.last_agent_timestamp = last_agent_ts if last_agent_ts is not None else {}
    dispatched_through = {}
    deps.last_timestamp = last_timestamp
    deps.agent_name = "Pynchy"
    deps.message_poll_interval = 0.0
    deps.message_data_dir = Path()
    deps.filter_allowed_messages = MagicMock(side_effect=lambda messages, *_args: messages)
    deps.linear_workspace_enabled = MagicMock(return_value=False)
    deps.create_linear_workspace_todo = AsyncMock()
    deps.channels = []  # empty by default; tests that need channel routing set this explicitly
    deps.routing_cursor = MagicMock(
        side_effect=lambda jid: max(
            deps.last_agent_timestamp.get(jid, ""),
            dispatched_through.get(jid, ""),
        )
    )
    deps.mark_dispatched = MagicMock(
        side_effect=lambda jid, timestamp: dispatched_through.__setitem__(
            jid,
            max(dispatched_through.get(jid, ""), timestamp),
        )
    )
    deps.pop_dispatched = MagicMock(side_effect=dispatched_through.pop)
    deps.dispatched_timestamp = MagicMock(side_effect=dispatched_through.get)

    # Async helpers
    deps.save_state = AsyncMock()
    deps.handle_context_reset = AsyncMock()
    deps.handle_end_session = AsyncMock()
    deps.trigger_manual_redeploy = AsyncMock()
    deps.broadcast_to_channels = AsyncMock()
    deps.broadcast_host_message = AsyncMock()
    deps.send_reaction_to_channels = AsyncMock()
    deps.send_reaction_to_outbound = AsyncMock()
    deps.processing_ack_emoji = MagicMock(return_value="🦞")
    deps.repo_is_dirty = MagicMock(return_value=False)
    deps.new_learning_run_summary = learning_capture.LearningRunSummary
    deps.observe_learning_output = learning_capture.observe_learning_output
    deps.set_typing_on_channels = AsyncMock()
    deps.emit = MagicMock()
    deps.start_interactive_turn = AsyncMock()
    deps.start_interrupted_turn = AsyncMock()

    async def successful_agent(*args, **_kwargs):
        await args[3](ContainerOutput(status="success", result="done"))
        return "success"

    deps.run_agent = AsyncMock(side_effect=successful_agent)
    deps.handle_streamed_output = AsyncMock(return_value=False)

    # Queue mock
    deps.queue = MagicMock()
    deps.queue.is_active_task = MagicMock(return_value=False)
    deps.queue.send_message = MagicMock(return_value=False)
    deps.queue.enqueue_message_check = MagicMock()
    deps.queue.clear_pending_tasks = MagicMock()
    deps.queue.stop_active_process = AsyncMock()
    deps.queue.destroy_runtime_session = AsyncMock()
    deps.queue.stop_active_process_for_control = AsyncMock()
    deps.queue.has_active_run = MagicMock(return_value=False)
    deps.queue.interrupt_after_tool_result = AsyncMock(return_value=False)
    deps.queue.close_stdin = MagicMock()

    return deps


def _make_group(
    *,
    name: str = "test-group",
    folder: str = "test-group",
    is_admin: bool = False,
) -> MagicMock:
    group = MagicMock(spec=WorkspaceProfile)
    group.name = name
    group.folder = folder
    group.is_admin = is_admin
    return group


def _make_message(
    content: str = "hello",
    *,
    message_id: str = "msg-1",
    chat_jid: str = "group@g.us",
    sender: str = "user@s.whatsapp.net",
    sender_name: str = "Alice",
    timestamp: str = "2024-01-01T00:00:01.000Z",
    is_from_me: bool | None = None,
    metadata: dict[str, object] | None = None,
) -> NewMessage:
    return NewMessage(
        id=message_id,
        chat_jid=chat_jid,
        sender=sender,
        sender_name=sender_name,
        content=content,
        timestamp=timestamp,
        is_from_me=is_from_me,
        metadata=metadata,
    )


async def _claimed_external_message(
    jid: str,
    group: WorkspaceProfile,
    *,
    suffix: str,
    provider: str = "matrix",
    public_source_input: bool | None = None,
) -> tuple[NewMessage, ExternalDeliveryIdentity]:
    identity = ExternalDeliveryIdentity(
        provider=ExternalProvider(provider),
        route=ExternalRoute("personal:family"),
        delivery_id=ExternalDeliveryId(f"$event-{suffix}"),
    )
    await admit_external_delivery_receipt(
        ExternalDeliveryReceipt(
            identity=identity,
            payload_sha256=f"sha-{suffix}",
            received_at="2026-07-19T12:00:00+00:00",
        )
    )
    admission = await admit_conversation_delivery(
        identity,
        ConversationSubject(
            namespace=ConversationSubjectNamespace("matrix:me:family:room"),
            key=ConversationSubjectKey("!family:example.com"),
        ),
        GroupFolder(group.folder),
    )
    claim_id = ConversationClaimId(f"claim-{suffix}")
    assert await claim_next_conversation_delivery(admission.conversation.id, claim_id)
    metadata: dict[str, object] = {
        "authenticated_external_route": True,
        "external_provider": provider,
        "conversation_id": admission.conversation.id,
        "conversation_claim_id": claim_id,
    }
    if public_source_input is not None:
        metadata["public_source_input"] = public_source_input
    message = _make_message(
        f"external input {suffix}",
        message_id=str(identity.delivery_id),
        chat_jid=jid,
        sender="@stranger:matrix.example.com",
        timestamp="2026-07-19T12:00:01+00:00",
        metadata=metadata,
    )
    await store_message(message)
    return message, identity


def _patch_intercept(*, return_value: bool = False):
    return patch(_P_INTERCEPT, new_callable=AsyncMock, return_value=return_value)


def _patch_msgs_since(messages: list):
    return patch(_P_MSGS_SINCE, new_callable=AsyncMock, return_value=messages)


def _patch_fmt_sdk():
    return patch(_P_FMT_SDK, return_value=[{"content": "hello"}])


def _dirty_notice_present(deps) -> bool:
    """True if run_agent received an 'uncommitted changes' system notice."""
    notices = deps.run_agent.call_args[0][4]
    return notices is not None and any("uncommitted" in n.lower() for n in notices)


def _observe_at_run(deps):
    """Install a run_agent side effect that snapshots state at dispatch time.

    process_group_messages marks the batch dispatched *before* invoking the
    agent and only advances/persists the cursor after it returns — so a
    run_agent side effect observes the in-flight dispatch state directly.
    """
    observed: dict = {}

    async def _capture(*_args, **_kwargs):
        await asyncio.sleep(0)
        observed["dispatched"] = deps.dispatched_timestamp("g@g.us")
        observed["cursor"] = deps.last_agent_timestamp.get("g@g.us")
        observed["saves"] = deps.save_state.await_count
        return "success"

    deps.run_agent = AsyncMock(side_effect=_capture)
    return observed


async def _run_with_observer(tmp_path, deps):
    msg = _make_message("hello", timestamp="new-ts")
    with (
        patch.object(deps, "message_data_dir", tmp_path),
        _patch_msgs_since([msg]),
        _patch_intercept(),
        _patch_fmt_sdk(),
    ):
        await process_group_messages(deps, "g@g.us")


def _reset_file(tmp_path):
    """Path to the reset_prompt.json the pipeline looks for (folder=test-group)."""
    path = tmp_path / "ipc" / "test-group" / "reset_prompt.json"
    path.parent.mkdir(parents=True)
    return path


def _run_loop_once(deps):
    """Run start_message_loop for exactly one iteration, then stop."""
    call_count = 0

    def shutting_down():
        nonlocal call_count
        call_count += 1
        # Let the loop body execute once (first check returns False),
        # then stop on the next check (returns True).
        return call_count > 1

    return start_message_loop(deps, shutting_down)
