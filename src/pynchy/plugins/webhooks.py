"""Typed plugin contract for authenticated external webhook routes."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from pynchy.conversation.api import (
    ConversationId,  # noqa: TC001 - beartype resolves lifecycle payloads.
    ConversationLifecycleFence,  # noqa: TC001 - beartype resolves lifecycle payloads.
    ConversationSubject,  # noqa: TC001 - beartype resolves webhook targets.
    ExternalDeliveryIdentity,  # noqa: TC001 - beartype resolves lifecycle payloads.
)
from pynchy.identifiers import (
    ChatJid,  # noqa: TC001 - beartype resolves contract annotations at runtime.
    GroupFolder,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)
from pynchy.logger import logger
from pynchy.webhook_effects import (  # noqa: TC001 - beartype resolves webhook evidence.
    WebhookEffectEvidence,
)
from pynchy.workspace.api import WorkspaceProfile

if TYPE_CHECKING:
    import pluggy


_ROUTE_COMPONENT = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
_ENV_REFERENCE = re.compile(r"[A-Z][A-Z0-9_]*")


class WebhookConfigurationError(ValueError):
    """Raised when a plugin exposes an unsafe or incomplete webhook route."""


class WebhookAuthenticationError(ValueError):
    """Raised when a provider request fails route-owned authentication."""


class WebhookPayloadError(ValueError):
    """Raised when an authenticated provider request has an invalid schema."""


class WebhookProcessingError(RuntimeError):
    """Raised when a trusted, idempotent host effect could not be completed."""


@dataclass(frozen=True)
class WebhookDiscard:
    """Authenticated provider delivery intentionally dropped before durable admission."""


@dataclass(frozen=True)
class WebhookConversation:
    """Provider-parsed placement for an existing routed conversation."""

    subject: ConversationSubject
    control_title: str
    control_closed: bool | None = None
    control_state_revision: str | None = None
    # Runtime ownership and provider controller placement are distinct.
    workspace: str | None = None
    controller_workspace: str | None = None
    public_source: bool | None = None
    notification_jid: ChatJid | None = None

    def __post_init__(self) -> None:
        if not self.control_title.strip():
            raise ValueError("Webhook conversation control title cannot be blank")
        if self.control_state_revision is not None and not self.control_state_revision.strip():
            raise ValueError("Webhook conversation control revision cannot be blank")
        if self.notification_jid is not None and not self.notification_jid.strip():
            raise ValueError("Webhook conversation notification JID cannot be blank")


WebhookExternalContext = str | Mapping[str, object]


@dataclass(frozen=True)
class WebhookLifecycle:
    """Provider-owned terminal work with durable callback retry.

    The optional context is durable route-owned data, not prompt content. The
    host records terminal intent and retires active routed work at ingress;
    its callback completes without starting an agent turn.
    """

    context: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.context is None:
            return
        try:
            json.dumps(self.context, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("Webhook lifecycle context must be JSON serializable") from exc


@dataclass(frozen=True)
class WebhookLifecycleDelivery:
    """Durable provider context supplied to a lifecycle callback."""

    identity: ExternalDeliveryIdentity
    conversation_id: ConversationId
    subject_id: str
    workspace: GroupFolder
    context: Mapping[str, object] | None
    lifecycle_fence: ConversationLifecycleFence | None = None


@dataclass(frozen=True)
class WebhookActor:
    """Provider-authenticated actor identity attached to one delivery."""

    id: str
    kind: str


@dataclass(frozen=True)
class WebhookEvent:
    """Closed provider event admitted by a plugin-owned route parser.

    A provider parser chooses exactly one disposition: an isolated agent task,
    a routed conversation turn, a lifecycle-only FIFO callback, a literal host
    notification, or an ignored delivery. Host notifications are for
    deterministic status updates only; provider text stays separate from host
    instructions. The route's source-trust declaration determines whether the
    host fences that context before dispatch.
    """

    delivery_id: str
    event_type: str
    action: str
    subject_id: str
    occurred_at: str
    instructions: str | None
    external_context: WebhookExternalContext | None
    ignored_reason: str | None = None
    host_message: str | None = None
    conversation: WebhookConversation | None = None
    actor: WebhookActor | None = None
    changed_fields: frozenset[str] = frozenset()
    lifecycle: WebhookLifecycle | None = None
    effect_evidence: WebhookEffectEvidence | None = None

    def __post_init__(self) -> None:
        if (self.instructions is None) != (self.external_context is None):
            raise ValueError("Actionable webhook events require instructions and context")
        actionable = bool(self.instructions) and self.external_context is not None
        if self.host_message is not None and not self.host_message.strip():
            raise ValueError("Webhook host notifications cannot be blank")
        lifecycle = self.lifecycle is not None
        if lifecycle and (self.instructions is not None or self.external_context is not None):
            raise ValueError("Lifecycle webhook events cannot carry prompt context")
        if lifecycle and (
            self.conversation is None or self.conversation.control_closed is not True
        ):
            raise ValueError("Lifecycle webhook events require a closed routed control")
        routed = actionable and self.conversation is not None and not lifecycle
        isolated = actionable and self.conversation is None
        dispositions = (
            isolated,
            routed,
            lifecycle,
            self.host_message is not None,
            bool(self.ignored_reason),
        )
        if sum(dispositions) != 1:
            raise ValueError(
                "Webhook event must be isolated, routed, lifecycle-only, notified, or ignored"
            )


WebhookParser = Callable[[bytes, Mapping[str, str], str, datetime], WebhookEvent | WebhookDiscard]
WebhookEventPreparer = Callable[[WebhookEvent], Awaitable[WebhookEvent]]
WebhookEventProcessor = Callable[[WebhookEvent], Awaitable[WebhookEvent]]
WebhookLifecycleProcessor = Callable[[WebhookLifecycleDelivery], Awaitable[None]]
WebhookWorkspaceValidator = Callable[[WorkspaceProfile], str | None]


@dataclass(frozen=True)
class WebhookRoute:
    """One externally reachable route with provider-owned parsing semantics."""

    provider: str
    name: str
    workspace: str | None
    secret_env: str
    parse: WebhookParser
    # NOTE: Update docs/plugins/hooks/webhooks.md and
    # docs/architecture/security.md "Authenticated external routes" if this changes.
    # Authentication proves provider origin. It does not decide whether text
    # carried by that provider can contain attacker-controlled content.
    public_source: bool = True
    validate_workspace: WebhookWorkspaceValidator | None = None
    max_body_bytes: int = 256 * 1024
    rate_limit_requests: int = 60
    rate_limit_window_seconds: int = 60
    prepare_event: WebhookEventPreparer | None = None
    process_event: WebhookEventProcessor | None = None
    routes_conversations: bool = False
    candidate_workspaces: tuple[str, ...] = ()
    allow_admin_workspaces: bool = False
    process_lifecycle: WebhookLifecycleProcessor | None = None

    @property
    def path(self) -> str:
        return f"/webhooks/{self.provider}/{self.name}"


def _validate_route(route: WebhookRoute) -> None:
    for label, value in (("provider", route.provider), ("name", route.name)):
        if not _ROUTE_COMPONENT.fullmatch(value):
            raise WebhookConfigurationError(
                f"Webhook route {label} must be a lowercase URL-safe identifier: {value!r}"
            )
    if route.workspace is not None and not route.workspace.strip():
        raise WebhookConfigurationError(f"Webhook route {route.path} has a blank workspace")
    if route.workspace is None and not route.candidate_workspaces:
        raise WebhookConfigurationError(f"Webhook route {route.path} has no workspace candidates")
    if not _ENV_REFERENCE.fullmatch(route.secret_env):
        raise WebhookConfigurationError(
            f"Webhook route {route.path} has an invalid secret environment reference"
        )
    if not os.environ.get(route.secret_env):
        raise WebhookConfigurationError(
            f"Webhook route {route.path} requires environment variable {route.secret_env}"
        )
    if route.max_body_bytes <= 0:
        raise WebhookConfigurationError(f"Webhook route {route.path} has no body-size budget")
    if route.rate_limit_requests <= 0 or route.rate_limit_window_seconds <= 0:
        raise WebhookConfigurationError(f"Webhook route {route.path} has an invalid rate limit")


def validate_webhook_routes(routes: Iterable[WebhookRoute]) -> tuple[WebhookRoute, ...]:
    """Validate route settings and reject ambiguous public paths."""
    validated = tuple(routes)
    for route in validated:
        _validate_route(route)
    paths = [route.path for route in validated]
    if len(paths) != len(set(paths)):
        raise WebhookConfigurationError("Webhook plugins registered duplicate public paths")
    return validated


def collect_webhook_routes(pm: pluggy.PluginManager) -> tuple[WebhookRoute, ...]:
    """Collect validated webhook routes and reject ambiguous public paths."""
    routes: list[WebhookRoute] = []
    for contribution in pm.hook.pynchy_webhook_routes():
        if contribution is None:
            continue
        candidates = contribution if isinstance(contribution, tuple | list) else (contribution,)
        for candidate in candidates:
            if not isinstance(candidate, WebhookRoute):
                logger.warning(
                    "Ignoring invalid webhook route plugin result",
                    result_type=type(candidate).__name__,
                )
                continue
            _validate_route(candidate)
            routes.append(candidate)

    return validate_webhook_routes(routes)
