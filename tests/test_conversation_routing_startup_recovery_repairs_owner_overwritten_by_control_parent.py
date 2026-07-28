"""Business tests for provider-neutral routed conversation foundations."""

from __future__ import annotations

from pynchy.conversation.dispatch import (
    register_conversation_delivery_waker,
    unregister_conversation_delivery_waker,
)
from pynchy.conversation.models import (
    ControlSurface,
    ConversationClaimId,
    ConversationControlBinding,
    ConversationDeliveryCompletion,
)
from pynchy.conversation.workspaces import routed_conversation_folder
from pynchy.host.orchestrator.conversation_control import (
    ConversationControlRequest,
    ensure_conversation_control,
)
from pynchy.host.orchestrator.session_handler import send_clear_confirmation
from pynchy.host.orchestrator.workspace_config import dynamic_thread_folder
from pynchy.identifiers import (
    ChatJid,
    GroupFolder,
    SessionId,
)
from pynchy.state import (
    claim_next_conversation_delivery,
    get_conversation,
    get_conversation_control_binding,
    get_session,
    get_workspace_profile,
    prepare_conversation_runtime_ownership_recovery,
    rebind_conversation_workspace,
    resolve_conversation,
    set_conversation_control_binding,
    set_conversation_session,
    set_session,
    set_workspace_profile,
    store_chat_metadata,
)
from pynchy.workspace.api import WorkspaceProfile
from tests.conversation_routing_support import (
    _admit,
    _bind_control_thread,
    _ClearConfirmationDeps,
    _DiscordThreadChannel,
    _register_workspace,
    _subject,
)

pytest_plugins = ("tests.conversation_routing_support",)


async def test_startup_recovery_repairs_owner_overwritten_by_control_parent() -> None:
    thread_jid = ChatJid("discord:channel:control-parent-owner")
    conversation = await resolve_conversation(
        _subject("issue-control-parent-owner"),
        GroupFolder("pynchy-dev"),
    )
    await store_chat_metadata(thread_jid, "2026-07-19T12:00:00+00:00")
    await set_conversation_control_binding(
        ConversationControlBinding(
            conversation_id=conversation.id,
            surface=ControlSurface.DISCORD,
            parent_workspace=GroupFolder("admin"),
            parent_jid=ChatJid("discord:channel:admin"),
            thread_jid=thread_jid,
            title="[SYN-35] Routed control",
            updated_at="2026-07-19T12:00:00+00:00",
        )
    )
    routed_folder = routed_conversation_folder("pynchy-dev", conversation.id)
    await set_workspace_profile(
        WorkspaceProfile(
            jid=thread_jid,
            name="Pynchy Dev/SYN-35",
            folder=routed_folder,
            trigger="@Pynchy",
        )
    )
    await rebind_conversation_workspace(conversation.id, GroupFolder("admin"))

    recovered = await prepare_conversation_runtime_ownership_recovery()

    repaired = await get_conversation(conversation.id)
    profile = await get_workspace_profile(thread_jid)
    assert recovered == 1
    assert repaired is not None
    assert repaired.workspace == GroupFolder("pynchy-dev")
    assert profile is not None
    assert profile.folder == routed_folder


async def test_startup_recovery_does_not_steal_routed_workspace_folder() -> None:
    thread_jid = ChatJid("discord:channel:thread-runtime-conflict")
    conversation = await resolve_conversation(
        _subject("issue-runtime-conflict"),
        GroupFolder("triage"),
    )
    await _bind_control_thread(conversation.id, thread_jid)
    legacy_folder = dynamic_thread_folder("triage", thread_jid)
    routed_folder = routed_conversation_folder("triage", conversation.id)
    legacy = WorkspaceProfile(
        jid=thread_jid,
        name="Legacy issue thread",
        folder=legacy_folder,
        trigger="@Pynchy",
    )
    existing = WorkspaceProfile(
        jid="discord:channel:other-runtime",
        name="Existing routed owner",
        folder=routed_folder,
        trigger="@Pynchy",
    )
    await set_workspace_profile(legacy)
    await set_workspace_profile(existing)
    session_id = SessionId("conflicting-runtime-session")
    await set_conversation_session(conversation.id, session_id)
    await set_session(GroupFolder(legacy_folder), session_id)

    recovered = await prepare_conversation_runtime_ownership_recovery()

    assert recovered == 0
    assert await get_workspace_profile(thread_jid) == legacy
    assert await get_workspace_profile(existing.jid) == existing
    assert await get_session(GroupFolder(legacy_folder)) == session_id
    assert await get_session(GroupFolder(routed_folder)) is None


async def test_reset_ack_precedes_wake_for_delivery_after_clear_boundary() -> None:
    thread_jid = ChatJid("discord:channel:thread-reset-wake")
    conversation = await resolve_conversation(
        _subject("issue-reset-wake"),
        GroupFolder("triage"),
    )
    await _bind_control_thread(conversation.id, thread_jid)
    await _admit(
        "delivery-reset-wake-before",
        "issue-reset-wake",
        received_at="2026-07-19T12:00:01+00:00",
    )
    retained = await _admit(
        "delivery-reset-wake-after",
        "issue-reset-wake",
        received_at="9999-01-01T00:00:00+00:00",
    )
    deps = _ClearConfirmationDeps()
    owner = object()

    async def wake_next(_completion: ConversationDeliveryCompletion) -> None:
        claimed = await claim_next_conversation_delivery(
            conversation.id,
            ConversationClaimId("claim-after-reset-ack"),
        )
        assert claimed is not None
        assert claimed.identity == retained.delivery.identity
        deps.events.append("wake")

    register_conversation_delivery_waker("linear", owner, wake_next)
    try:
        await send_clear_confirmation(deps, thread_jid)
    finally:
        unregister_conversation_delivery_waker("linear", owner)

    assert deps.events == ["cleared", "ack", "wake"]


async def test_deleted_control_thread_is_replaced_and_workspace_can_rebind() -> None:
    triage_jid = ChatJid("discord:channel:triage")
    engineering_jid = ChatJid("discord:channel:engineering")
    await _register_workspace(triage_jid, "triage")
    await _register_workspace(engineering_jid, "engineering")
    conversation = await resolve_conversation(_subject("issue-42"), GroupFolder("triage"))
    await set_conversation_session(conversation.id, SessionId("session-42"))
    channel = _DiscordThreadChannel()
    readable_title = "[ENG-42] Repair scheduler recovery"

    first = await ensure_conversation_control(
        [channel],
        ConversationControlRequest(
            conversation_id=conversation.id,
            parent_workspace=GroupFolder("triage"),
            parent_jid=triage_jid,
            title=readable_title,
        ),
    )
    del channel.threads[triage_jid, readable_title]
    replacement = await ensure_conversation_control(
        [channel],
        ConversationControlRequest(
            conversation_id=conversation.id,
            parent_workspace=GroupFolder("triage"),
            parent_jid=triage_jid,
            title=readable_title,
        ),
    )
    moved = await ensure_conversation_control(
        [channel],
        ConversationControlRequest(
            conversation_id=conversation.id,
            parent_workspace=GroupFolder("engineering"),
            parent_jid=engineering_jid,
            title=readable_title,
        ),
    )

    current = await get_conversation(conversation.id)
    binding = await get_conversation_control_binding(conversation.id)
    assert first.binding.thread_jid != replacement.binding.thread_jid
    assert replacement.binding.thread_jid != moved.binding.thread_jid
    assert binding == moved.binding
    assert binding.title == readable_title
    assert conversation.id not in binding.title
    assert current is not None
    assert current.id == conversation.id
    assert current.workspace == GroupFolder("engineering")
    assert current.session_id == SessionId("session-42")


async def test_control_titles_fit_discord_and_remain_idempotent() -> None:
    parent_jid = ChatJid("discord:channel:triage")
    await _register_workspace(parent_jid, "triage")
    conversation = await resolve_conversation(_subject("long-title"), GroupFolder("triage"))
    channel = _DiscordThreadChannel()
    title = "[SYN-13] " + "operational error log " * 8
    request = ConversationControlRequest(
        conversation_id=conversation.id,
        parent_workspace=GroupFolder("triage"),
        parent_jid=parent_jid,
        title=title,
    )

    first = await ensure_conversation_control([channel], request)
    second = await ensure_conversation_control([channel], request)

    assert len(first.binding.title) == 100
    assert second.binding.thread_jid == first.binding.thread_jid
    assert len(channel.created) == 1


async def test_control_titles_use_human_friendly_suffixes_for_distinct_conversations() -> None:
    parent_jid = ChatJid("discord:channel:triage")
    await _register_workspace(parent_jid, "triage")
    first = await resolve_conversation(_subject("family-route"), GroupFolder("triage"))
    second = await resolve_conversation(_subject("renamed-family-route"), GroupFolder("triage"))
    channel = _DiscordThreadChannel()

    first_control = await ensure_conversation_control(
        [channel],
        ConversationControlRequest(
            conversation_id=first.id,
            parent_workspace=GroupFolder("triage"),
            parent_jid=parent_jid,
            title="Family",
        ),
    )
    renamed_route_control = await ensure_conversation_control(
        [channel],
        ConversationControlRequest(
            conversation_id=second.id,
            parent_workspace=GroupFolder("triage"),
            parent_jid=parent_jid,
            title="Family",
        ),
    )

    assert first_control.binding.title == "Family"
    assert renamed_route_control.binding.title == "Family (2)"
    assert first_control.binding.thread_jid != renamed_route_control.binding.thread_jid
    assert [created[1] for created in channel.created] == ["Family", "Family (2)"]
    assert "conv_" not in renamed_route_control.binding.title
