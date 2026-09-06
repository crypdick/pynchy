"""Route authenticated webhook events into durable conversation workspaces."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pynchy.conversation.api import (
    ConversationClaimId,
    ConversationDelivery,
    ConversationDeliveryCompletion,
    ConversationId,
    ExternalProvider,
    ExternalRoute,
    conversation_runtime_lock,
    notify_conversation_delivery_completed,
    register_conversation_delivery_waker,
    routed_conversation_folder,
    unregister_conversation_delivery_waker,
)
from pynchy.host.orchestrator.conversation_control import sync_conversation_control_state
from pynchy.host.orchestrator.webhook_conversation_admission import (
    conversation_admission_request,
    process_deferred_event,
)
from pynchy.host.orchestrator.webhook_delivery_processing import (
    WebhookDeliveryDeps,
    complete_lifecycle_delivery,
    complete_webhook_delivery,
    prepare_webhook_message,
    restore_runtime_workspace,
)
from pynchy.host.orchestrator.webhook_terminal_retirement import (
    TerminalConversationRecoveryDeps,
    recover_terminal_conversation,
    retire_terminal_runtime,
)
from pynchy.host.orchestrator.workspace_config import (
    RuntimeWorkspacePolicy,
    register_runtime_workspace_policy,
    unregister_runtime_workspace_policy,
)
from pynchy.identifiers import GroupFolder
from pynchy.logger import logger
from pynchy.plugins.api import (
    NewMessage,
    WebhookEvent,
    WebhookRoute,
)
from pynchy.state.api import (
    WebhookAdmission,
    WebhookReceipt,
    admit_webhook_conversation,
    apply_conversation_control_state,
    claim_next_conversation_delivery,
    list_idle_conversation_ids,
    list_pending_conversation_ids,
    list_route_conversation_ids,
    release_conversation_delivery_claim,
    resolve_conversation,
)
from pynchy.workspace.api import (
    WorkspaceProfile,
)


@runtime_checkable
class ConversationWebhookDeps(
    TerminalConversationRecoveryDeps,
    WebhookDeliveryDeps,
    Protocol,
):
    """Host capabilities required only by routed webhook events."""

    def get_workspace(self, folder: str) -> WorkspaceProfile | None: ...

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
    _terminal_cleanup_conversations: set[ConversationId] = field(
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
                await self.restore_existing_open_control_runtime(conversation_id)
                await self._recover_terminal_runtime(conversation_id)
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
            unregister_runtime_workspace_policy(folder)
        self._runtime_workspace_folders.clear()
        self._terminal_cleanup_conversations.clear()

    async def project_open_control(
        self,
        route: WebhookRoute,
        event: WebhookEvent,
    ) -> ConversationId | None:
        """Apply a verified nonterminal provider snapshot before receipt admission."""
        target = event.conversation
        if target is None or target.control_closed is not False:
            return None
        workspace = target.workspace or route.workspace
        if workspace is None:
            raise RuntimeError("Routed webhook conversation has no workspace owner")
        conversation = await resolve_conversation(target.subject, GroupFolder(workspace))
        if await apply_conversation_control_state(
            conversation.id,
            closed=False,
            control_state_revision=target.control_state_revision,
        ):
            self._terminal_cleanup_conversations.discard(conversation.id)
            return conversation.id
        return None

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
        if (
            admission.conversation is not None
            and admission.conversation.terminal_retirement is not None
            and await retire_terminal_runtime(
                self.deps,
                admission.conversation.conversation.id,
                admission.conversation.terminal_retirement,
                self._runtime_workspace_folders,
            )
        ):
            self._terminal_cleanup_conversations.add(admission.conversation.conversation.id)
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
        completion: ConversationDeliveryCompletion | None = None
        if processed_delivery is None:
            if delivery.payload is not None and delivery.payload.get("control_closed") is False:
                await self.restore_existing_open_control_runtime(delivery.conversation_id)
            completion = await complete_webhook_delivery(
                self.deps,
                claim_id,
                allow_missing_claim=True,
            )
            if completion is not None:
                await notify_conversation_delivery_completed(completion)
            return
        delivery = processed_delivery
        if (
            self._is_lifecycle_delivery(delivery)
            and delivery.conversation_id not in self._terminal_cleanup_conversations
        ):
            await self._recover_terminal_runtime(delivery.conversation_id)
        async with conversation_runtime_lock(delivery.conversation_id):
            if self._is_lifecycle_delivery(delivery):
                completion = await complete_lifecycle_delivery(
                    self.deps,
                    self._route_for_delivery(delivery),
                    delivery,
                    claim_id,
                )
            else:
                prepared_message = await prepare_webhook_message(
                    self.deps,
                    delivery,
                    claim_id,
                    self._register_runtime_workspace_policy,
                )
                if prepared_message is None:
                    completion = await complete_webhook_delivery(
                        self.deps,
                        claim_id,
                        allow_missing_claim=True,
                    )
                else:
                    workspace_jid, message = prepared_message
                    await self.deps.ingest_message(workspace_jid, message)
        if completion is not None:
            await notify_conversation_delivery_completed(completion)

    @staticmethod
    def _is_lifecycle_delivery(delivery: ConversationDelivery) -> bool:
        payload = delivery.payload
        return payload is not None and payload.get("delivery_mode") == "lifecycle"

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
        async with conversation_runtime_lock(conversation_id):
            try:
                await sync_conversation_control_state(self.deps.channels(), conversation_id)
            except Exception:  # noqa: BLE001 - durable intent is retried at startup.
                logger.exception(
                    "Conversation control lifecycle sync failed",
                    provider=route.provider,
                    route=route.name,
                    conversation_id=conversation_id,
                )

    async def _recover_terminal_runtime(
        self,
        conversation_id: ConversationId,
    ) -> bool:
        """Finish durable terminal cleanup interrupted after ingress state commit."""
        if conversation_id in self._terminal_cleanup_conversations:
            return True
        recovered = await recover_terminal_conversation(
            self.deps,
            conversation_id,
            self._runtime_workspace_folders,
        )
        if recovered:
            self._terminal_cleanup_conversations.add(conversation_id)
        return recovered

    async def restore_existing_open_control_runtime(
        self,
        conversation_id: ConversationId,
    ) -> None:
        """Register an unarchived existing control without creating a thread."""
        async with conversation_runtime_lock(conversation_id):
            await restore_runtime_workspace(
                self.deps,
                conversation_id,
                self._register_runtime_workspace_policy,
            )

    def _register_runtime_workspace_policy(
        self,
        conversation_id: ConversationId,
        workspace: GroupFolder,
        policy_owner: str,
    ) -> None:
        folder = routed_conversation_folder(workspace, conversation_id)
        register_runtime_workspace_policy(
            folder,
            RuntimeWorkspacePolicy(parent_workspace=policy_owner),
        )
        self._runtime_workspace_folders.add(folder)
