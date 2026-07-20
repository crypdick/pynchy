"""Route authenticated webhook events into durable conversation workspaces."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pynchy.conversation.dispatch import (
    register_conversation_delivery_waker,
    unregister_conversation_delivery_waker,
)
from pynchy.conversation.models import (
    ConversationClaimId,
    ConversationDelivery,
    ConversationDeliveryCompletion,
    ConversationId,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.host.orchestrator.conversation_control import (
    ConversationControlRequest,
    ConversationWorkspaceContext,
    ensure_conversation_workspace,
)
from pynchy.plugins.webhooks import (  # noqa: TC001, RUF100 - beartype resolves dispatcher inputs.
    WebhookEvent,
    WebhookRoute,
)
from pynchy.state import (
    admit_conversation_delivery,
    claim_next_conversation_delivery,
    get_conversation,
    get_conversation_control_binding,
    list_pending_conversation_ids,
    release_conversation_delivery_claim,
)
from pynchy.types import (
    Channel,
    ChatJid,
    GroupFolder,
    NewMessage,
    SessionId,
    WorkspaceProfile,
)


@runtime_checkable
class ConversationWebhookDeps(Protocol):
    """Host capabilities required only by routed webhook events."""

    def get_workspace(self, folder: str) -> WorkspaceProfile | None: ...

    def channels(self) -> list[Channel]: ...

    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    async def register_workspace(self, profile: WorkspaceProfile) -> None: ...

    async def unregister_workspace(self, jid: str) -> None: ...

    async def bind_session(self, folder: str, session_id: SessionId) -> None: ...

    async def ingest_message(self, jid: str, message: NewMessage) -> None: ...


@dataclass(frozen=True, slots=True)
class WebhookConversationDispatcher:
    """Own conversation admission, placement, claims, and wake recovery."""

    deps: ConversationWebhookDeps
    routes: tuple[WebhookRoute, ...]
    owner: object = field(default_factory=object)

    async def start(self) -> None:
        """Register completion wakes and recover pending deliveries."""
        for provider in {route.provider for route in self.routes}:
            register_conversation_delivery_waker(provider, self.owner, self.after_completion)
        for route in self.routes:
            pending = await list_pending_conversation_ids(
                ExternalProvider(route.provider),
                ExternalRoute(route.name),
            )
            for conversation_id in pending:
                await self.wake(conversation_id)

    def close(self) -> None:
        """Remove this HTTP runtime's process-local completion callbacks."""
        for provider in {route.provider for route in self.routes}:
            unregister_conversation_delivery_waker(provider, self.owner)

    async def admit(
        self,
        route: WebhookRoute,
        event: WebhookEvent,
        prompt: str,
    ) -> ConversationId | None:
        """Idempotently link one routed event to its immutable subject."""
        target = event.conversation
        if target is None:
            return None
        identity = ExternalDeliveryIdentity(
            provider=ExternalProvider(route.provider),
            route=ExternalRoute(route.name),
            delivery_id=ExternalDeliveryId(event.delivery_id),
        )
        admission = await admit_conversation_delivery(
            identity,
            target.subject,
            GroupFolder(route.workspace),
            payload={
                "prompt": prompt,
                "control_title": target.control_title,
                "event_type": event.event_type,
                "event_action": event.action,
            },
        )
        return admission.conversation.id

    async def wake(self, conversation_id: ConversationId) -> None:
        """Claim and inject the next FIFO delivery, if the conversation is idle."""
        claim_id = ConversationClaimId(f"webhook_{secrets.token_urlsafe(18)}")
        delivery = await claim_next_conversation_delivery(conversation_id, claim_id)
        if delivery is None:
            return
        try:
            workspace_jid, message = await self._prepare_message(delivery, claim_id)
            await self.deps.ingest_message(workspace_jid, message)
        except BaseException:
            await release_conversation_delivery_claim(claim_id)
            raise

    async def after_completion(self, completed: ConversationDeliveryCompletion) -> None:
        """Wake only a pending sibling owned by this route registry."""
        owned_routes = {(route.provider, route.name) for route in self.routes}
        if (completed.identity.provider, completed.identity.route) in owned_routes:
            await self.wake(completed.conversation_id)

    async def _prepare_message(
        self,
        delivery: ConversationDelivery,
        claim_id: ConversationClaimId,
    ) -> tuple[str, NewMessage]:
        payload = delivery.payload or {}
        prompt = payload.get("prompt")
        proposed_title = payload.get("control_title")
        if not isinstance(prompt, str) or not isinstance(proposed_title, str):
            raise TypeError("Routed webhook delivery lost its host-parsed prompt")

        conversation = await get_conversation(delivery.conversation_id)
        if conversation is None:
            raise RuntimeError("Routed webhook delivery references a missing conversation")
        parent = self.deps.get_workspace(conversation.workspace)
        if parent is None:
            raise RuntimeError("Routed webhook conversation lost its parent workspace")
        binding = await get_conversation_control_binding(conversation.id)
        title = binding.title if binding is not None else proposed_title
        workspace = await ensure_conversation_workspace(
            ConversationWorkspaceContext(
                channels=self.deps.channels,
                workspaces=self.deps.workspaces,
                register_workspace=self.deps.register_workspace,
                unregister_workspace=self.deps.unregister_workspace,
                bind_session=self.deps.bind_session,
            ),
            ConversationControlRequest(
                conversation_id=conversation.id,
                parent_workspace=conversation.workspace,
                parent_jid=ChatJid(parent.jid),
                title=title,
            ),
        )
        return workspace.profile.jid, NewMessage(
            id=str(delivery.identity.delivery_id),
            chat_jid=workspace.control.binding.thread_jid,
            sender=f"{delivery.identity.provider}-webhook",
            sender_name=delivery.identity.provider.title(),
            content=prompt,
            timestamp=delivery.received_at,
            is_from_me=False,
            metadata={
                "authenticated_external_route": True,
                "external_provider": delivery.identity.provider,
                "webhook_route": delivery.identity.route,
                "conversation_id": conversation.id,
                "conversation_claim_id": claim_id,
            },
        )
