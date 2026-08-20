"""Public behavior tests for routed webhook delivery processing."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest
from conftest import configure_workspace_placement_for, init_test_database, make_settings

from pynchy.conversation.api import (
    ControlSurface,
    Conversation,
    ConversationClaimId,
    ConversationControlBinding,
    ConversationDelivery,
    ConversationDeliveryCompletion,
    ConversationDeliveryStatus,
    ConversationId,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.host.orchestrator.conversation_control import (
    ConversationControlWorkspaceChangedError,
    EnsuredConversationControl,
    EnsuredConversationWorkspace,
)
from pynchy.host.orchestrator.webhook_delivery_processing import (
    complete_lifecycle_delivery,
    complete_webhook_delivery,
    prepare_webhook_message,
    restore_runtime_workspace,
)
from pynchy.identifiers import ChatJid, GroupFolder
from pynchy.plugins.api import WebhookRoute
from pynchy.workspace.api import WorkspaceProfile


def _identity(delivery_id: str = "delivery-1") -> ExternalDeliveryIdentity:
    return ExternalDeliveryIdentity(
        provider=ExternalProvider("linear"),
        route=ExternalRoute("project"),
        delivery_id=ExternalDeliveryId(delivery_id),
    )


def _conversation(*, closed: bool = False) -> Conversation:
    return Conversation(
        id=ConversationId("conversation-1"),
        workspace=GroupFolder("project"),
        subject=ConversationSubject(
            namespace=ConversationSubjectNamespace("linear:tenant:issue"),
            key=ConversationSubjectKey("issue-1"),
        ),
        session_id=None,
        created_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:00:00+00:00",
        control_closed=closed,
        control_state_revision="revision-1" if closed else None,
    )


def _binding() -> ConversationControlBinding:
    return ConversationControlBinding(
        conversation_id=ConversationId("conversation-1"),
        surface=ControlSurface.DISCORD,
        parent_workspace=GroupFolder("project"),
        parent_jid=ChatJid("discord:channel:project"),
        thread_jid=ChatJid("discord:channel:thread-1"),
        title="[PYN-1] Existing title",
        updated_at="2026-07-30T00:00:00+00:00",
    )


def _delivery(payload: dict[str, object] | None = None) -> ConversationDelivery:
    return ConversationDelivery(
        sequence=1,
        identity=_identity(),
        conversation_id=ConversationId("conversation-1"),
        status=ConversationDeliveryStatus.CLAIMED,
        received_at="2026-07-30T00:00:00+00:00",
        payload=payload,
        claim_id=ConversationClaimId("claim-1"),
    )


def _route(process_lifecycle=None) -> WebhookRoute:
    return WebhookRoute(
        provider="linear",
        name="project",
        workspace="project",
        secret_env="LINEAR_SECRET",  # pragma: allowlist secret  # noqa: S106
        parse=lambda *_args: None,
        public_source=False,
        routes_conversations=True,
        process_lifecycle=process_lifecycle,
    )


def _profile() -> WorkspaceProfile:
    return WorkspaceProfile(
        jid="discord:channel:project",
        name="Project",
        folder="project",
        trigger="@Pynchy",
        added_at="2026-07-30T00:00:00+00:00",
    )


@dataclass
class _Deps:
    conversation: Conversation | None = None
    binding: ConversationControlBinding | None = None
    completed: ConversationDelivery | None = None
    state_matches: list[bool] | None = None

    def __post_init__(self) -> None:
        self.workspace_map = {_profile().jid: _profile()}
        self.channels_list: list[object] = []
        self.registered: list[WorkspaceProfile] = []

    def channels(self) -> list[object]:
        return self.channels_list

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return self.workspace_map

    async def register_workspace(self, profile: WorkspaceProfile) -> None:
        self.registered.append(profile)
        self.workspace_map[profile.jid] = profile

    async def unregister_workspace(self, jid: str) -> None:
        self.workspace_map.pop(jid, None)

    async def bind_session(self, folder: str, session_id: object) -> None:
        del folder, session_id

    async def complete_conversation_delivery(
        self, claim_id: ConversationClaimId
    ) -> ConversationDelivery | None:
        assert claim_id == ConversationClaimId("claim-1")
        return self.completed

    async def conversation_control_state_matches(self, *_args, **_kwargs) -> bool:
        if self.state_matches is None:
            return True
        return self.state_matches.pop(0)

    async def get_conversation(self, _conversation_id: ConversationId) -> Conversation | None:
        return self.conversation

    async def get_conversation_control_binding(
        self, _conversation_id: ConversationId
    ) -> ConversationControlBinding | None:
        return self.binding


async def test_complete_webhook_delivery_rejects_a_lost_claim() -> None:
    with pytest.raises(RuntimeError, match="lost its FIFO claim"):
        await complete_webhook_delivery(_Deps(), ConversationClaimId("claim-1"))


async def test_complete_webhook_delivery_allows_a_retired_claim() -> None:
    assert (
        await complete_webhook_delivery(
            _Deps(), ConversationClaimId("claim-1"), allow_missing_claim=True
        )
        is None
    )


async def test_complete_webhook_delivery_returns_provider_neutral_completion() -> None:
    completed = _delivery()
    deps = _Deps(completed=completed)

    assert await complete_webhook_delivery(deps, ConversationClaimId("claim-1")) == (
        ConversationDeliveryCompletion(
            identity=completed.identity,
            conversation_id=completed.conversation_id,
        )
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "must close its routed control"),
        (
            {"control_closed": True, "control_state_revision": 1},
            "lost its control lifecycle revision",
        ),
        ({"control_closed": True, "subject_id": ""}, "lost its provider subject"),
        (
            {"control_closed": True, "subject_id": "issue-1", "lifecycle_context": []},
            "invalid provider context",
        ),
    ],
)
async def test_lifecycle_delivery_rejects_invalid_payload(
    payload: dict[str, object], message: str
) -> None:
    with pytest.raises(TypeError, match=message):
        await complete_lifecycle_delivery(
            _Deps(), _route(), _delivery(payload), ConversationClaimId("claim-1")
        )


async def test_lifecycle_delivery_skips_stale_claim() -> None:
    deps = _Deps(state_matches=[False], completed=None)

    assert (
        await complete_lifecycle_delivery(
            deps,
            _route(),
            _delivery({"control_closed": True, "subject_id": "issue-1"}),
            ConversationClaimId("claim-1"),
        )
        is None
    )


async def test_lifecycle_delivery_runs_callback_and_completes_claim() -> None:
    callback = AsyncMock()
    conversation = _conversation(closed=True)
    completed = _delivery()
    deps = _Deps(conversation=conversation, completed=completed, state_matches=[True, True])

    with patch(
        "pynchy.host.orchestrator.webhook_delivery_processing.sync_conversation_control_state",
        AsyncMock(),
    ):
        result = await complete_lifecycle_delivery(
            deps,
            _route(callback),
            _delivery(
                {
                    "control_closed": True,
                    "control_state_revision": "revision-1",
                    "subject_id": "issue-1",
                    "lifecycle_context": {"state": "done"},
                }
            ),
            ConversationClaimId("claim-1"),
        )

    assert result is not None
    callback.assert_awaited_once()
    received = callback.await_args.args[0]
    assert received.subject_id == "issue-1"
    assert received.workspace == GroupFolder("project")
    assert received.context == {"state": "done"}
    assert received.lifecycle_fence.control_state_revision == "revision-1"


async def test_lifecycle_delivery_retries_after_archive_failure() -> None:
    callback = AsyncMock()
    deps = _Deps(conversation=_conversation(closed=True), state_matches=[True, True])
    archive_error = RuntimeError("archive failed")
    with (
        patch(
            "pynchy.host.orchestrator.webhook_delivery_processing.sync_conversation_control_state",
            AsyncMock(side_effect=archive_error),
        ),
        pytest.raises(RuntimeError, match="archive failed"),
    ):
        await complete_lifecycle_delivery(
            deps,
            _route(callback),
            _delivery({"control_closed": True, "subject_id": "issue-1"}),
            ConversationClaimId("claim-1"),
        )
    callback.assert_awaited_once()


async def test_lifecycle_delivery_requires_the_conversation_to_exist() -> None:
    deps = _Deps(state_matches=[True])
    with pytest.raises(RuntimeError, match="missing conversation"):
        await complete_lifecycle_delivery(
            deps,
            _route(),
            _delivery({"control_closed": True, "subject_id": "issue-1"}),
            ConversationClaimId("claim-1"),
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"control_title": "title"}, "lost its host-parsed prompt"),
        (
            {"prompt": "prompt", "control_title": "title", "control_closed": "no"},
            "lost its control lifecycle state",
        ),
        (
            {"prompt": "prompt", "control_title": "title", "control_state_revision": ""},
            "lost its control lifecycle revision",
        ),
        (
            {"prompt": "prompt", "control_title": "title", "public_source": "yes"},
            "lost its source trust",
        ),
        (
            {"prompt": "prompt", "control_title": "title", "human_derived": "yes"},
            "lost its actor provenance",
        ),
    ],
)
async def test_prepare_webhook_message_rejects_invalid_payload(
    payload: dict[str, object], message: str
) -> None:
    with pytest.raises(TypeError, match=message):
        await prepare_webhook_message(
            _Deps(conversation=_conversation()),
            _delivery(payload),
            ConversationClaimId("claim-1"),
            lambda *_args: None,
        )


async def test_prepare_webhook_message_skips_stale_or_terminal_delivery() -> None:
    stale = _Deps(conversation=_conversation(), state_matches=[False])
    assert (
        await prepare_webhook_message(
            stale,
            _delivery(
                {
                    "prompt": "prompt",
                    "control_title": "title",
                    "control_closed": False,
                    "control_state_revision": "revision-1",
                }
            ),
            ConversationClaimId("claim-1"),
            lambda *_args: None,
        )
        is None
    )

    assert (
        await prepare_webhook_message(
            _Deps(conversation=_conversation(closed=True)),
            _delivery({"prompt": "prompt", "control_title": "title"}),
            ConversationClaimId("claim-1"),
            lambda *_args: None,
        )
        is None
    )


async def test_prepare_webhook_message_projects_current_control_workspace(tmp_path) -> None:
    await init_test_database()
    configure_workspace_placement_for(make_settings(groups_dir=tmp_path))
    deps = _Deps(conversation=_conversation())
    registered: list[tuple[ConversationId, GroupFolder, str]] = []
    ensured = EnsuredConversationWorkspace(
        profile=WorkspaceProfile(
            jid="discord:channel:thread-1",
            name="Project/Title",
            folder="project__thread_conversation-1",
            trigger="@Pynchy",
            added_at="2026-07-30T00:00:00+00:00",
        ),
        control=EnsuredConversationControl(binding=_binding(), created=False),
    )
    with patch(
        "pynchy.host.orchestrator.webhook_delivery_processing.ensure_conversation_workspace",
        AsyncMock(return_value=ensured),
    ):
        chat_jid, message = await prepare_webhook_message(
            deps,
            _delivery(
                {
                    "prompt": "Handle update",
                    "control_title": "Ignored title",
                    "public_source": False,
                }
            ),
            ConversationClaimId("claim-1"),
            lambda *args: registered.append(args),
        )

    assert chat_jid == "discord:channel:thread-1"
    assert message.content == "Handle update"
    assert message.sender == "linear-webhook"
    assert message.chat_jid == chat_jid
    assert message.metadata["public_source_input"] is False
    assert message.metadata["conversation_claim_id"] == ConversationClaimId("claim-1")
    assert registered == [(ConversationId("conversation-1"), GroupFolder("project"), "project")]


async def test_paused_webhook_thread_drops_automation_but_accepts_human_comment(tmp_path) -> None:
    configure_workspace_placement_for(make_settings(groups_dir=tmp_path))
    deps = _Deps(conversation=_conversation())
    ensured = EnsuredConversationWorkspace(
        profile=WorkspaceProfile(
            jid="discord:channel:thread-1",
            name="Project/Title",
            folder="project__thread_conversation-1",
            trigger="@Pynchy",
        ),
        control=EnsuredConversationControl(binding=_binding(), created=False),
    )
    automated = _delivery(
        {
            "prompt": "GitHub check failed",
            "control_title": "Title",
            "human_derived": False,
        }
    )
    human = _delivery(
        {
            "prompt": "Author: Operator\nComment: continue",
            "control_title": "Title",
            "human_derived": True,
        }
    )

    with (
        patch(
            "pynchy.host.orchestrator.webhook_delivery_processing.ensure_conversation_workspace",
            AsyncMock(return_value=ensured),
        ),
        patch(
            "pynchy.host.orchestrator.webhook_delivery_processing.is_chat_paused",
            AsyncMock(return_value=True),
        ),
        patch(
            "pynchy.host.orchestrator.webhook_delivery_processing.clear_chat_pause",
            AsyncMock(),
        ) as clear_chat_pause,
    ):
        assert (
            await prepare_webhook_message(
                deps,
                automated,
                ConversationClaimId("claim-1"),
                lambda *_args: None,
            )
            is None
        )
        prepared = await prepare_webhook_message(
            deps,
            human,
            ConversationClaimId("claim-1"),
            lambda *_args: None,
        )

    assert prepared is not None
    assert prepared[1].metadata["human_derived"] is True
    clear_chat_pause.assert_awaited_once_with("discord:channel:thread-1")


async def test_prepare_webhook_message_retries_workspace_change_once(tmp_path) -> None:
    await init_test_database()
    configure_workspace_placement_for(make_settings(groups_dir=tmp_path))
    deps = _Deps(conversation=_conversation())
    ensured = EnsuredConversationWorkspace(
        profile=_profile(),
        control=EnsuredConversationControl(binding=_binding(), created=False),
    )
    ensure = AsyncMock(side_effect=[ConversationControlWorkspaceChangedError("changed"), ensured])
    with patch(
        "pynchy.host.orchestrator.webhook_delivery_processing.ensure_conversation_workspace",
        ensure,
    ):
        result = await prepare_webhook_message(
            deps,
            _delivery({"prompt": "prompt", "control_title": "title"}),
            ConversationClaimId("claim-1"),
            lambda *_args: None,
        )
    assert result is not None
    assert ensure.await_count == 2


async def test_prepare_webhook_message_raises_if_workspace_never_stabilizes(tmp_path) -> None:
    configure_workspace_placement_for(make_settings(groups_dir=tmp_path))
    deps = _Deps(conversation=_conversation())
    ensure = AsyncMock(side_effect=ConversationControlWorkspaceChangedError("changed"))
    with (
        patch(
            "pynchy.host.orchestrator.webhook_delivery_processing.ensure_conversation_workspace",
            ensure,
        ),
        pytest.raises(ConversationControlWorkspaceChangedError, match="changed"),
    ):
        await prepare_webhook_message(
            deps,
            _delivery({"prompt": "prompt", "control_title": "title"}),
            ConversationClaimId("claim-1"),
            lambda *_args: None,
        )


async def test_prepare_webhook_message_rejects_missing_conversation_or_placement(tmp_path) -> None:
    configure_workspace_placement_for(make_settings(groups_dir=tmp_path))
    with pytest.raises(RuntimeError, match="missing conversation"):
        await prepare_webhook_message(
            _Deps(),
            _delivery({"prompt": "prompt", "control_title": "title"}),
            ConversationClaimId("claim-1"),
            lambda *_args: None,
        )
    missing_placement = _Deps(conversation=_conversation())
    missing_placement.workspace_map.clear()
    with pytest.raises(RuntimeError, match="lost its workspace placement"):
        await prepare_webhook_message(
            missing_placement,
            _delivery({"prompt": "prompt", "control_title": "title"}),
            ConversationClaimId("claim-1"),
            lambda *_args: None,
        )


async def test_restore_runtime_workspace_handles_missing_terminal_and_unplaced_conversation(
    tmp_path,
) -> None:
    configure_workspace_placement_for(make_settings(groups_dir=tmp_path))
    register = AsyncMock()
    with pytest.raises(RuntimeError, match="missing conversation"):
        await restore_runtime_workspace(_Deps(), ConversationId("conversation-1"), register)
    await restore_runtime_workspace(
        _Deps(conversation=_conversation(closed=True)), ConversationId("conversation-1"), register
    )
    unplaced = _Deps(conversation=_conversation())
    unplaced.workspace_map.clear()
    await restore_runtime_workspace(unplaced, ConversationId("conversation-1"), register)
    register.assert_not_awaited()


async def test_restore_runtime_workspace_registers_policy_and_recovers_binding(tmp_path) -> None:
    configure_workspace_placement_for(make_settings(groups_dir=tmp_path))
    deps = _Deps(conversation=_conversation(), binding=_binding())
    registered: list[tuple[ConversationId, GroupFolder, str]] = []
    with patch(
        "pynchy.host.orchestrator.webhook_delivery_processing.ensure_conversation_workspace",
        AsyncMock(),
    ) as ensure:
        await restore_runtime_workspace(
            deps,
            ConversationId("conversation-1"),
            lambda *args: registered.append(args),
        )
    assert registered == [(ConversationId("conversation-1"), GroupFolder("project"), "project")]
    ensure.assert_awaited_once()


async def test_restore_runtime_workspace_survives_stale_control_recovery_failure(tmp_path) -> None:
    configure_workspace_placement_for(make_settings(groups_dir=tmp_path))
    deps = _Deps(conversation=_conversation(), binding=_binding())
    with patch(
        "pynchy.host.orchestrator.webhook_delivery_processing.ensure_conversation_workspace",
        AsyncMock(side_effect=RuntimeError("stale thread")),
    ):
        await restore_runtime_workspace(
            deps,
            ConversationId("conversation-1"),
            lambda *_args: None,
        )
