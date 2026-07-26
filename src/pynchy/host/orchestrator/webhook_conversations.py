"""Route authenticated webhook events into durable conversation workspaces."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from pynchy.conversation.dispatch import (
    notify_conversation_delivery_completed,
    register_conversation_delivery_waker,
    unregister_conversation_delivery_waker,
)
from pynchy.conversation.models import (
    ConversationClaimId,
    ConversationDelivery,
    ConversationDeliveryCompletion,
    ConversationId,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.conversation.workspaces import routed_conversation_folder
from pynchy.host.orchestrator.conversation_control import (
    ConversationControlRequest,
    ConversationWorkspaceContext,
    ensure_conversation_workspace,
    sync_conversation_control_state,
)
from pynchy.host.orchestrator.webhook_conversation_admission import (
    conversation_admission_request,
    process_deferred_event,
)
from pynchy.host.orchestrator.workspace_config import (
    RuntimeWorkspaceRestriction,
    register_runtime_workspace_restriction,
    unregister_runtime_workspace_restriction,
)
from pynchy.host.orchestrator.workspace_placement import resolve_workspace_placement
from pynchy.logger import logger
from pynchy.plugins.webhooks import (  # noqa: TC001, RUF100 - beartype resolves dispatcher inputs.
    WebhookEvent,
    WebhookLifecycleDelivery,
    WebhookRoute,
)
from pynchy.state import (
    WebhookAdmission,
    WebhookReceipt,
    admit_conversation_delivery,
    admit_webhook_conversation,
    claim_next_conversation_delivery,
    complete_conversation_delivery,
    get_conversation,
    get_conversation_control_binding,
    list_idle_conversation_ids,
    list_pending_conversation_ids,
    list_route_conversation_ids,
    release_conversation_delivery_claim,
)
from pynchy.state.conversation_controls import close_conversation_control
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
    _runtime_workspace_folders: set[str] = field(
        default_factory=set,
        compare=False,
        repr=False,
    )

    def prepare(self) -> None:
        """Register process-local completion wakes without dispatching work."""
        for provider in {route.provider for route in self.routes}:
            register_conversation_delivery_waker(provider, self.owner, self.after_completion)

    async def recover_pending(self) -> None:
        """Restore durable routes and wake pending deliveries."""
        for route in self.routes:
            provider = ExternalProvider(route.provider)
            route_id = ExternalRoute(route.name)
            for conversation_id in await list_route_conversation_ids(provider, route_id):
                await self._restore_runtime_workspace(conversation_id)
            for conversation_id in await list_idle_conversation_ids(provider, route_id):
                await self._sync_control_state(route, conversation_id)
            pending = await list_pending_conversation_ids(
                provider,
                route_id,
            )
            for conversation_id in pending:
                await self.wake(conversation_id)

    async def start(self) -> None:
        """Prepare callbacks and recover pending deliveries."""
        self.prepare()
        await self.recover_pending()

    def close(self) -> None:
        """Remove this HTTP runtime's process-local completion callbacks."""
        for provider in {route.provider for route in self.routes}:
            unregister_conversation_delivery_waker(provider, self.owner)
        for folder in self._runtime_workspace_folders:
            unregister_runtime_workspace_restriction(folder)
        self._runtime_workspace_folders.clear()

    async def admit(
        self,
        route: WebhookRoute,
        event: WebhookEvent,
        prompt: str | None,
    ) -> ConversationId | None:
        """Idempotently link one routed event to its immutable subject."""
        request = conversation_admission_request(route, event, prompt)
        if request is None:
            return None
        admission = await admit_conversation_delivery(
            request.identity,
            request.subject,
            request.workspace,
            payload=request.payload,
        )
        if admission is None:
            return None
        return admission.conversation.id

    async def admit_webhook(
        self,
        route: WebhookRoute,
        event: WebhookEvent,
        prompt: str | None,
        receipt: WebhookReceipt,
        *,
        defer_process_event: bool,
    ) -> tuple[WebhookAdmission, ConversationId | None]:
        """Atomically link an authenticated receipt to its parsed FIFO entry."""
        request = conversation_admission_request(
            route,
            event,
            prompt,
            defer_process_event=defer_process_event,
        )
        if request is None:
            raise ValueError("Conversation webhook lost its parsed route target")
        admission = await admit_webhook_conversation(
            receipt,
            request,
            effect_evidence=event.effect_evidence,
        )
        conversation_id = (
            admission.conversation.conversation.id if admission.conversation is not None else None
        )
        return admission.webhook, conversation_id

    async def wake(self, conversation_id: ConversationId) -> None:
        """Claim and inject the next FIFO delivery, if the conversation is idle."""
        claim_id = ConversationClaimId(f"webhook_{secrets.token_urlsafe(18)}")
        delivery = await claim_next_conversation_delivery(conversation_id, claim_id)
        if delivery is None:
            return
        try:
            await self._dispatch_claimed_delivery(delivery, claim_id)
        except BaseException:
            await release_conversation_delivery_claim(claim_id)
            raise

    async def _dispatch_claimed_delivery(
        self,
        delivery: ConversationDelivery,
        claim_id: ConversationClaimId,
    ) -> None:
        processed_delivery = await process_deferred_event(
            delivery,
            claim_id,
            self._route_for_delivery(delivery),
        )
        if processed_delivery is None:
            return
        delivery = processed_delivery
        if self._is_lifecycle_delivery(delivery):
            await self._complete_lifecycle_delivery(delivery, claim_id)
            return
        workspace_jid, message = await self._prepare_message(delivery, claim_id)
        await self.deps.ingest_message(workspace_jid, message)

    @staticmethod
    def _is_lifecycle_delivery(delivery: ConversationDelivery) -> bool:
        payload = delivery.payload
        return payload is not None and payload.get("delivery_mode") == "lifecycle"

    async def _complete_lifecycle_delivery(
        self,
        delivery: ConversationDelivery,
        claim_id: ConversationClaimId,
    ) -> None:
        """Run a route lifecycle callback without constructing an agent turn.

        The claim makes this the conversation's FIFO head.  The durable close
        occurs before the route-owned side effect; a failed callback releases
        the claim through ``wake`` so startup or a replay can retry it.
        """
        payload = delivery.payload or {}
        proposed_closed = payload.get("control_closed")
        subject_id = payload.get("subject_id")
        context = payload.get("lifecycle_context")
        if proposed_closed is not True:
            raise TypeError("Lifecycle webhook delivery must close its routed control")
        if not isinstance(subject_id, str) or not subject_id:
            raise TypeError("Lifecycle webhook delivery lost its provider subject")
        if context is not None and not isinstance(context, Mapping):
            raise TypeError("Lifecycle webhook delivery has an invalid provider context")

        route = self._route_for_delivery(delivery)
        conversation = await get_conversation(delivery.conversation_id)
        if conversation is None:
            raise RuntimeError("Lifecycle webhook delivery references a missing conversation")

        await close_conversation_control(conversation.id)
        await self._sync_control_state(route, conversation.id)
        if route.process_lifecycle is not None:
            await route.process_lifecycle(
                WebhookLifecycleDelivery(
                    identity=delivery.identity,
                    conversation_id=conversation.id,
                    subject_id=subject_id,
                    workspace=conversation.workspace,
                    context=context,
                )
            )

        completed = await complete_conversation_delivery(claim_id)
        if completed is None:
            raise RuntimeError("Lifecycle webhook delivery lost its FIFO claim")
        await notify_conversation_delivery_completed(
            ConversationDeliveryCompletion(
                identity=completed.identity,
                conversation_id=completed.conversation_id,
            )
        )

    def _route_for_delivery(self, delivery: ConversationDelivery) -> WebhookRoute:
        for route in self.routes:
            if (route.provider, route.name) == (
                delivery.identity.provider,
                delivery.identity.route,
            ):
                return route
        raise RuntimeError("Lifecycle webhook delivery belongs to an unavailable route")

    async def after_completion(self, completed: ConversationDeliveryCompletion) -> None:
        """Wake only a pending sibling owned by this route registry."""
        owned_routes = {(route.provider, route.name) for route in self.routes}
        if (completed.identity.provider, completed.identity.route) in owned_routes:
            route = next(
                route
                for route in self.routes
                if (route.provider, route.name)
                == (completed.identity.provider, completed.identity.route)
            )
            await self._sync_control_state(route, completed.conversation_id)
            await self.wake(completed.conversation_id)

    async def _sync_control_state(
        self,
        route: WebhookRoute,
        conversation_id: ConversationId,
    ) -> None:
        try:
            await sync_conversation_control_state(self.deps.channels(), conversation_id)
        except Exception:  # noqa: BLE001, RUF100 - durable intent is retried at startup.
            logger.exception(
                "Conversation control lifecycle sync failed",
                provider=route.provider,
                route=route.name,
                conversation_id=conversation_id,
            )

    async def _prepare_message(
        self,
        delivery: ConversationDelivery,
        claim_id: ConversationClaimId,
    ) -> tuple[str, NewMessage]:
        payload = delivery.payload or {}
        prompt = payload.get("prompt")
        proposed_title = payload.get("control_title")
        proposed_closed = payload.get("control_closed")
        public_source = payload.get("public_source", True)
        if not isinstance(prompt, str) or not isinstance(proposed_title, str):
            raise TypeError("Routed webhook delivery lost its host-parsed prompt")
        if proposed_closed is not None and not isinstance(proposed_closed, bool):
            raise TypeError("Routed webhook delivery lost its control lifecycle state")
        if not isinstance(public_source, bool):
            raise TypeError("Routed webhook delivery lost its source trust")

        conversation = await get_conversation(delivery.conversation_id)
        if conversation is None:
            raise RuntimeError("Routed webhook delivery references a missing conversation")
        placement = resolve_workspace_placement(
            self.deps.workspaces().values(),
            conversation.workspace,
        )
        if placement is None:
            raise RuntimeError("Routed webhook conversation lost its workspace placement")
        self._register_runtime_workspace_policy(
            conversation.id,
            conversation.workspace,
            placement.owner.folder,
        )
        binding = await get_conversation_control_binding(conversation.id)
        title = binding.title if binding is not None else proposed_title
        closed = (
            proposed_closed
            if proposed_closed is not None
            else binding.closed
            if binding is not None
            else False
        )
        workspace = await ensure_conversation_workspace(
            ConversationWorkspaceContext(
                channels=self.deps.channels,
                workspaces=self.deps.workspaces,
                register_workspace=self.deps.register_workspace,
                unregister_workspace=self.deps.unregister_workspace,
                bind_session=self.deps.bind_session,
                rebind_workspace=getattr(self.deps, "rebind_workspace", None),
            ),
            ConversationControlRequest(
                conversation_id=conversation.id,
                parent_workspace=GroupFolder(placement.control_parent.folder),
                parent_jid=ChatJid(placement.control_parent.jid),
                title=title,
                owner_workspace=conversation.workspace,
                closed=closed,
            ),
        )
        return workspace.profile.jid, NewMessage(
            id=str(delivery.identity.delivery_id),
            chat_jid=workspace.control.binding.thread_jid,
            sender=f"{delivery.identity.provider}-webhook",
            sender_name=delivery.identity.provider.title(),
            content=prompt,
            # Recovery can wake a durable delivery after this chat's message
            # cursor has passed its provider receipt time. Stamp the local wake
            # so the ordinary ingestion loop cannot skip the recovered message.
            timestamp=datetime.now(UTC).isoformat(),
            is_from_me=False,
            metadata={
                "authenticated_external_route": True,
                "public_source_input": public_source,
                "external_provider": delivery.identity.provider,
                "webhook_route": delivery.identity.route,
                "conversation_id": conversation.id,
                "conversation_claim_id": claim_id,
            },
        )

    async def _restore_runtime_workspace(
        self,
        conversation_id: ConversationId,
    ) -> None:
        """Restore a bound routed workspace before channel messages can arrive."""
        conversation = await get_conversation(conversation_id)
        if conversation is None:
            raise RuntimeError("Routed webhook delivery references a missing conversation")
        placement = resolve_workspace_placement(
            self.deps.workspaces().values(),
            conversation.workspace,
        )
        if placement is None:
            logger.warning(
                "Routed webhook conversation lost its workspace policy",
                conversation_id=conversation_id,
                workspace=conversation.workspace,
            )
            return
        self._register_runtime_workspace_policy(
            conversation.id,
            conversation.workspace,
            placement.owner.folder,
        )
        binding = await get_conversation_control_binding(conversation.id)
        if binding is None:
            return
        try:
            await ensure_conversation_workspace(
                ConversationWorkspaceContext(
                    channels=self.deps.channels,
                    workspaces=self.deps.workspaces,
                    register_workspace=self.deps.register_workspace,
                    unregister_workspace=self.deps.unregister_workspace,
                    bind_session=self.deps.bind_session,
                    rebind_workspace=getattr(self.deps, "rebind_workspace", None),
                ),
                ConversationControlRequest(
                    conversation_id=conversation.id,
                    parent_workspace=binding.parent_workspace,
                    parent_jid=binding.parent_jid,
                    title=binding.title,
                    owner_workspace=conversation.workspace,
                    closed=binding.closed,
                ),
            )
        except Exception:  # noqa: BLE001, RUF100 - one stale provider control must not prevent webhook startup.
            logger.exception(
                "Routed webhook workspace registration recovery failed",
                conversation_id=conversation_id,
                workspace=conversation.workspace,
            )

    def _register_runtime_workspace_policy(
        self,
        conversation_id: ConversationId,
        workspace: GroupFolder,
        policy_owner: str,
    ) -> None:
        folder = routed_conversation_folder(workspace, conversation_id)
        register_runtime_workspace_restriction(
            folder,
            RuntimeWorkspaceRestriction(parent_workspace=policy_owner),
        )
        self._runtime_workspace_folders.add(folder)
