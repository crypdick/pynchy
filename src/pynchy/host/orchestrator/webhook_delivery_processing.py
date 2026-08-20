"""Deliver claimed webhook work and restore its routed runtime workspace."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from pynchy.conversation.api import (
    Conversation,
    ConversationClaimId,
    ConversationControlBinding,
    ConversationDelivery,
    ConversationDeliveryCompletion,
    ConversationId,
    ConversationLifecycleFence,
    ExternalDeliveryIdentity,
)
from pynchy.host.orchestrator.conversation_control import (
    ConversationControlRequest,
    ConversationControlWorkspaceChangedError,
    ConversationWorkspaceContext,
    ensure_conversation_workspace,
    sync_conversation_control_state,
)
from pynchy.host.orchestrator.workspace_placement import resolve_workspace_placement
from pynchy.identifiers import (
    ChatJid,
    GroupFolder,
    SessionId,
)
from pynchy.logger import logger
from pynchy.plugins.api import (
    Channel,
    NewMessage,
    WebhookLifecycleDelivery,
    WebhookRoute,
)
from pynchy.state.api import clear_chat_pause, is_chat_paused
from pynchy.workspace.api import (
    WorkspaceProfile,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)

RuntimeWorkspacePolicyRegistrar = Callable[[ConversationId, GroupFolder, str], None]


@dataclass(frozen=True)
class _WebhookMessagePayload:
    prompt: str
    title: str
    closed: bool | None
    control_state_revision: str | None
    public_source: bool
    human_derived: bool


def _webhook_message_payload(delivery: ConversationDelivery) -> _WebhookMessagePayload:
    payload = delivery.payload or {}
    prompt = payload.get("prompt")
    title = payload.get("control_title")
    closed = payload.get("control_closed")
    revision = payload.get("control_state_revision")
    public_source = payload.get("public_source", True)
    human_derived = payload.get("human_derived", False)
    if not isinstance(prompt, str) or not isinstance(title, str):
        raise TypeError("Routed webhook delivery lost its host-parsed prompt")
    if closed is not None and not isinstance(closed, bool):
        raise TypeError("Routed webhook delivery lost its control lifecycle state")
    if revision is not None and (not isinstance(revision, str) or not revision):
        raise TypeError("Routed webhook delivery lost its control lifecycle revision")
    if not isinstance(public_source, bool):
        raise TypeError("Routed webhook delivery lost its source trust")
    if not isinstance(human_derived, bool):
        raise TypeError("Routed webhook delivery lost its actor provenance")
    return _WebhookMessagePayload(
        prompt,
        title,
        closed,
        revision,
        public_source,
        human_derived,
    )


async def _accept_webhook_for_chat(
    chat_jid: str,
    delivery: ConversationDelivery,
    *,
    human_derived: bool,
) -> bool:
    if not await is_chat_paused(chat_jid):
        return True
    if human_derived:
        await clear_chat_pause(chat_jid)
        return True
    logger.info(
        "Dropped automated webhook for paused chat",
        chat_jid=chat_jid,
        provider=delivery.identity.provider,
    )
    return False


@runtime_checkable
class WebhookDeliveryDeps(Protocol):
    """Host callbacks needed after a routed delivery holds its FIFO claim."""

    def channels(self) -> list[Channel]: ...

    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    async def register_workspace(self, profile: WorkspaceProfile) -> None: ...

    async def unregister_workspace(self, jid: str) -> None: ...

    async def bind_session(self, folder: str, session_id: SessionId) -> None: ...

    async def complete_conversation_delivery(
        self, claim_id: ConversationClaimId
    ) -> ConversationDelivery | None: ...

    async def conversation_control_state_matches(
        self,
        conversation_id: ConversationId,
        *,
        closed: bool,
        control_state_revision: str | None,
        delivery_identity: ExternalDeliveryIdentity | None = None,
        claim_id: ConversationClaimId | None = None,
    ) -> bool: ...

    async def get_conversation(self, conversation_id: ConversationId) -> Conversation | None: ...

    async def get_conversation_control_binding(
        self, conversation_id: ConversationId
    ) -> ConversationControlBinding | None: ...


def _workspace_context(deps: WebhookDeliveryDeps) -> ConversationWorkspaceContext:
    return ConversationWorkspaceContext(
        channels=deps.channels,
        workspaces=deps.workspaces,
        register_workspace=deps.register_workspace,
        unregister_workspace=deps.unregister_workspace,
        bind_session=deps.bind_session,
        rebind_workspace=getattr(deps, "rebind_workspace", None),
    )


async def complete_webhook_delivery(
    deps: WebhookDeliveryDeps,
    claim_id: ConversationClaimId,
    *,
    allow_missing_claim: bool = False,
) -> ConversationDeliveryCompletion | None:
    """Complete a claimed delivery; caller notifies after releasing runtime work."""
    completed = await deps.complete_conversation_delivery(claim_id)
    if completed is None:
        if allow_missing_claim:
            return None
        raise RuntimeError("Webhook delivery lost its FIFO claim")
    return ConversationDeliveryCompletion(
        identity=completed.identity,
        conversation_id=completed.conversation_id,
    )


async def complete_lifecycle_delivery(
    deps: WebhookDeliveryDeps,
    route: WebhookRoute,
    delivery: ConversationDelivery,
    claim_id: ConversationClaimId,
) -> ConversationDeliveryCompletion | None:
    """Run a lifecycle callback only while its terminal revision and claim win."""
    payload = delivery.payload or {}
    proposed_closed = payload.get("control_closed")
    control_state_revision = payload.get("control_state_revision")
    subject_id = payload.get("subject_id")
    context = payload.get("lifecycle_context")
    if proposed_closed is not True:
        raise TypeError("Lifecycle webhook delivery must close its routed control")
    if control_state_revision is not None and (
        not isinstance(control_state_revision, str) or not control_state_revision
    ):
        raise TypeError("Lifecycle webhook delivery lost its control lifecycle revision")
    if not isinstance(subject_id, str) or not subject_id:
        raise TypeError("Lifecycle webhook delivery lost its provider subject")
    if context is not None and not isinstance(context, Mapping):
        raise TypeError("Lifecycle webhook delivery has an invalid provider context")

    if not await deps.conversation_control_state_matches(
        delivery.conversation_id,
        closed=True,
        control_state_revision=control_state_revision,
        delivery_identity=delivery.identity,
        claim_id=claim_id,
    ):
        return await complete_webhook_delivery(deps, claim_id, allow_missing_claim=True)

    conversation = await deps.get_conversation(delivery.conversation_id)
    if conversation is None:
        raise RuntimeError("Lifecycle webhook delivery references a missing conversation")
    archive_error: Exception | None = None
    try:
        await sync_conversation_control_state(deps.channels(), conversation.id)
    except Exception as exc:  # noqa: BLE001 - preserve retry after local retirement.
        logger.exception(
            "Conversation control archive failed",
            provider=route.provider,
            route=route.name,
            conversation_id=conversation.id,
        )
        archive_error = exc
    if not await deps.conversation_control_state_matches(
        delivery.conversation_id,
        closed=True,
        control_state_revision=control_state_revision,
        delivery_identity=delivery.identity,
        claim_id=claim_id,
    ):
        return await complete_webhook_delivery(deps, claim_id, allow_missing_claim=True)
    if route.process_lifecycle is not None:
        await route.process_lifecycle(
            WebhookLifecycleDelivery(
                identity=delivery.identity,
                conversation_id=conversation.id,
                subject_id=subject_id,
                workspace=conversation.workspace,
                context=context,
                lifecycle_fence=ConversationLifecycleFence(
                    conversation_id=conversation.id,
                    identity=delivery.identity,
                    claim_id=claim_id,
                    # The state layer stores normalized provider revisions. Reuse
                    # that canonical value for later atomic work-item settlement.
                    control_state_revision=conversation.control_state_revision,
                ),
            )
        )
    if archive_error is not None:
        raise archive_error

    # A newer terminal delivery can retire this claim while a route-owned
    # callback is reconciling. Its guarded local effect already became a no-op.
    return await complete_webhook_delivery(deps, claim_id, allow_missing_claim=True)


async def prepare_webhook_message(
    deps: WebhookDeliveryDeps,
    delivery: ConversationDelivery,
    claim_id: ConversationClaimId,
    register_runtime_workspace_policy: RuntimeWorkspacePolicyRegistrar,
) -> tuple[str, NewMessage] | None:
    """Project a current nonterminal delivery into its control workspace."""
    message_payload = _webhook_message_payload(delivery)

    attempt = 0
    while True:
        conversation = await deps.get_conversation(delivery.conversation_id)
        if conversation is None:
            raise RuntimeError("Routed webhook delivery references a missing conversation")
        if message_payload.closed is False and message_payload.control_state_revision is not None:
            if not await deps.conversation_control_state_matches(
                conversation.id,
                closed=False,
                control_state_revision=message_payload.control_state_revision,
                delivery_identity=delivery.identity,
                claim_id=claim_id,
            ):
                return None
        elif conversation.control_closed:
            # Comments and generic stale callbacks must not recreate a terminal control.
            return None
        placement = resolve_workspace_placement(deps.workspaces().values(), conversation.workspace)
        if placement is None:
            raise RuntimeError("Routed webhook conversation lost its workspace placement")
        binding = await deps.get_conversation_control_binding(conversation.id)
        title = binding.title if binding is not None else message_payload.title
        try:
            workspace = await ensure_conversation_workspace(
                _workspace_context(deps),
                ConversationControlRequest(
                    conversation_id=conversation.id,
                    parent_workspace=GroupFolder(placement.control_parent.folder),
                    parent_jid=ChatJid(placement.control_parent.jid),
                    title=title,
                    owner_workspace=conversation.workspace,
                ),
            )
        except ConversationControlWorkspaceChangedError:
            if attempt == 1:
                raise
            attempt = 1
            continue
        register_runtime_workspace_policy(
            conversation.id,
            conversation.workspace,
            placement.owner.folder,
        )
        thread_jid = str(workspace.control.binding.thread_jid)
        if not await _accept_webhook_for_chat(
            thread_jid,
            delivery,
            human_derived=message_payload.human_derived,
        ):
            return None
        return workspace.profile.jid, NewMessage(
            id=str(delivery.identity.delivery_id),
            chat_jid=workspace.control.binding.thread_jid,
            sender=f"{delivery.identity.provider}-webhook",
            sender_name=delivery.identity.provider.title(),
            content=message_payload.prompt,
            # Recovery can wake a durable delivery after this chat's message
            # cursor has passed its provider receipt time. Stamp the local wake
            # so the ordinary ingestion loop cannot skip the recovered message.
            timestamp=datetime.now(UTC).isoformat(),
            is_from_me=False,
            metadata={
                "authenticated_external_route": True,
                "public_source_input": message_payload.public_source,
                "human_derived": message_payload.human_derived,
                "external_provider": delivery.identity.provider,
                "webhook_route": delivery.identity.route,
                "conversation_id": conversation.id,
                "conversation_claim_id": claim_id,
            },
        )


async def restore_runtime_workspace(
    deps: WebhookDeliveryDeps,
    conversation_id: ConversationId,
    register_runtime_workspace_policy: RuntimeWorkspacePolicyRegistrar,
) -> None:
    """Restore a bound routed workspace while caller holds its runtime lock."""
    conversation = await deps.get_conversation(conversation_id)
    if conversation is None:
        raise RuntimeError("Routed webhook delivery references a missing conversation")
    if conversation.control_closed:
        return
    placement = resolve_workspace_placement(deps.workspaces().values(), conversation.workspace)
    if placement is None:
        logger.warning(
            "Routed webhook conversation lost its workspace policy",
            conversation_id=conversation_id,
            workspace=conversation.workspace,
        )
        return
    register_runtime_workspace_policy(
        conversation.id,
        conversation.workspace,
        placement.owner.folder,
    )
    binding = await deps.get_conversation_control_binding(conversation.id)
    if binding is None:
        return
    try:
        await ensure_conversation_workspace(
            _workspace_context(deps),
            ConversationControlRequest(
                conversation_id=conversation.id,
                parent_workspace=binding.parent_workspace,
                parent_jid=binding.parent_jid,
                title=binding.title,
                owner_workspace=conversation.workspace,
            ),
        )
    except Exception:  # noqa: BLE001 - one stale provider control must not prevent webhook startup.
        logger.exception(
            "Routed webhook workspace registration recovery failed",
            conversation_id=conversation_id,
            workspace=conversation.workspace,
        )
