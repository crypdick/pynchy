"""Typed plugin contract for authenticated external webhook routes."""

from __future__ import annotations

import os
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from pynchy.conversation.models import (
    ConversationSubject,  # noqa: TC001, RUF100 - beartype resolves webhook targets.
)
from pynchy.logger import logger
from pynchy.types import WorkspaceProfile

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
class WebhookConversation:
    """Provider-parsed placement for an actionable routed conversation event."""

    subject: ConversationSubject
    control_title: str
    control_closed: bool | None = None
    workspace: str | None = None
    public_source: bool | None = None

    def __post_init__(self) -> None:
        if not self.control_title.strip():
            raise ValueError("Webhook conversation control title cannot be blank")


WebhookExternalContext = str | Mapping[str, object]


@dataclass(frozen=True)
class WebhookEvent:
    """Closed provider event admitted by a plugin-owned route parser.

    A provider parser chooses exactly one disposition: an isolated agent task,
    a routed conversation turn, a literal host notification, or an ignored
    delivery. Host notifications are for deterministic status updates only;
    provider text stays separate from host instructions. The route's source-trust
    declaration determines whether the host fences that context before dispatch.
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

    def __post_init__(self) -> None:
        if (self.instructions is None) != (self.external_context is None):
            raise ValueError("Actionable webhook events require instructions and context")
        actionable = bool(self.instructions) and self.external_context is not None
        if self.host_message is not None and not self.host_message.strip():
            raise ValueError("Webhook host notifications cannot be blank")
        routed = actionable and self.conversation is not None
        isolated = actionable and self.conversation is None
        dispositions = (
            isolated,
            routed,
            self.host_message is not None,
            bool(self.ignored_reason),
        )
        if sum(dispositions) != 1:
            raise ValueError("Webhook event must be isolated, routed, notified, or ignored")


WebhookParser = Callable[[bytes, Mapping[str, str], str, datetime], WebhookEvent]
WebhookEventPreparer = Callable[[WebhookEvent], Awaitable[WebhookEvent]]
WebhookEventProcessor = Callable[[WebhookEvent], Awaitable[WebhookEvent]]
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
