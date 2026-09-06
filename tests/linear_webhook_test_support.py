"""Reusable authenticated Linear webhook HTTP test harness."""

from __future__ import annotations

import hashlib
import hmac
import json
from functools import partial
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

from pynchy.host.orchestrator.http_control import (
    ControlPlaneRuntime,
    ControlPlaneToken,
    RequestRateLimiter,
)
from pynchy.plugins.api import (
    InboundFetchResult,
    NewMessage,
    OutboundEvent,
    WebhookRoute,
)
from pynchy.plugins.integrations.linear_webhooks import (
    LinearWebhookRouteConfig,
    parse_linear_webhook,
)
from pynchy.state import (
    complete_conversation_delivery,
    conversation_control_state_matches,
    delete_workspace_profile,
    get_conversation,
    get_conversation_control_binding,
    get_terminal_conversation_retirement,
    set_workspace_profile,
)
from pynchy.state import (
    retire_conversation_for_terminal as retire_terminal_state,
)
from pynchy.workspace.api import WorkspaceProfile

if TYPE_CHECKING:
    from datetime import datetime

    from aiohttp.test_utils import TestClient

    from pynchy.conversation.models import (
        ConversationId,
        ExternalDeliveryIdentity,
        TerminalConversationRetirement,
    )
    from pynchy.identifiers import SessionId
    from pynchy.scheduling.api import ScheduledTask

SIGNING_KEY = "linear-webhook-test-signing-key-long-enough"
DELIVERY_ID = "234d1a4e-b617-4388-90fe-adc3633d6b72"
SECOND_DELIVERY_ID = "8c4071d2-71d6-4f41-b121-608c72be1ba0"
THIRD_DELIVERY_ID = "bfd820e7-3d16-4a66-8ec6-4b289075ccf1"
ORGANIZATION_ID = "org-1"


def payload(
    *,
    now: datetime,
    event_type: str = "Comment",
    action: str = "create",
    data: dict[str, Any] | None = None,
    updated_from: dict[str, Any] | None = None,
    url: str = "https://linear.app/acme/issue/PYN-1#comment-comment-1",
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "action": action,
        "actor": {"id": "user-1", "type": "user", "name": "Example User"},
        "data": data
        or {
            "id": "comment-1",
            "issueId": "issue-1",
            "body": "please review this",
        },
        "type": event_type,
        "url": url,
        "createdAt": now.isoformat(),
        "organizationId": ORGANIZATION_ID,
        "webhookTimestamp": int(now.timestamp() * 1000),
    }
    if updated_from is not None:
        value["updatedFrom"] = updated_from
    return value


def signed_request(
    body: dict[str, Any],
    *,
    delivery_id: str = DELIVERY_ID,
) -> tuple[bytes, dict[str, str]]:
    raw_body = json.dumps(body, separators=(",", ":")).encode()
    signature = hmac.new(SIGNING_KEY.encode(), raw_body, hashlib.sha256).hexdigest()
    return raw_body, {
        "Content-Type": "application/json",
        "Linear-Delivery": delivery_id,
        "Linear-Event": str(body["type"]),
        "Linear-Signature": signature,
        "Linear-Timestamp": str(body["webhookTimestamp"]),
    }


def route_config() -> LinearWebhookRouteConfig:
    return LinearWebhookRouteConfig(
        name="project",
        workspace="project",
        organization_id=ORGANIZATION_ID,
    )


def webhook_route() -> WebhookRoute:
    config = route_config()
    return WebhookRoute(
        provider="linear",
        name=config.name,
        workspace=config.workspace,
        secret_env=config.secret_env,
        parse=partial(parse_linear_webhook, config=config),
        public_source=False,
        routes_conversations=True,
    )


class DiscordThreadChannel:
    name = "connection.discord.main"
    formatter = object()

    def __init__(self) -> None:
        self.threads: dict[tuple[str, str], str] = {}
        self.created: list[tuple[str, str, str]] = []
        self.closed: dict[str, bool] = {}

    async def connect(self) -> None: ...

    async def send_event(self, jid: str, event: OutboundEvent) -> None:
        del jid, event

    def is_connected(self) -> bool:
        return True

    def owns_jid(self, jid: str) -> bool:
        return jid.startswith("discord:channel:")

    async def disconnect(self) -> None: ...

    async def reconnect(self) -> None: ...

    def prepare_shutdown(self) -> None: ...

    async def fetch_inbound_since(self, channel_jid: str, since: str) -> InboundFetchResult:
        del channel_jid, since
        return InboundFetchResult(messages=[])

    async def find_thread(self, parent_jid: str, name: str) -> str | None:
        jid = self.threads.get((parent_jid, name))
        if jid is not None:
            self.closed[jid] = False
        return jid

    async def create_thread(
        self,
        parent_jid: str,
        name: str,
        *,
        participant_ids: tuple[str, ...] = (),
    ) -> str:
        assert participant_ids == ()
        jid = f"discord:channel:linear-thread-{len(self.created) + 1}"
        self.threads[parent_jid, name] = jid
        self.closed[jid] = False
        self.created.append((parent_jid, name, jid))
        return jid

    async def set_thread_closed(self, child_jid: str, *, closed: bool) -> None:
        if child_jid not in self.closed:
            raise RuntimeError(f"Unknown test thread: {child_jid}")
        self.closed[child_jid] = closed


class LinearWebhookHarness:
    def __init__(self, *, admin: bool = False) -> None:
        self.broadcast_synthetic_user_input = AsyncMock()
        self.workspace = WorkspaceProfile(
            jid="discord:channel:project",
            name="Project",
            folder="project",
            trigger="@Pynchy",
            is_admin=admin,
        )
        self.channel = DiscordThreadChannel()
        self.workspace_map = {self.workspace.jid: self.workspace}
        self.dispatched: list[ScheduledTask] = []
        self.ingested: list[NewMessage] = []
        self.bound_sessions: list[tuple[str, SessionId]] = []
        self.retired_folders: list[str] = []
        self.retired_task_conversations: list[str] = []
        self.capability_status_operations = object()
        self.deploy_operations = object()
        self.canary_run_to_dict = lambda *_args, **_kwargs: {}
        self.work_item_execution_to_dict = lambda *_args, **_kwargs: {}

    complete_conversation_delivery = staticmethod(complete_conversation_delivery)
    conversation_control_state_matches = staticmethod(conversation_control_state_matches)
    get_conversation = staticmethod(get_conversation)
    get_conversation_control_binding = staticmethod(get_conversation_control_binding)
    get_terminal_conversation_retirement = staticmethod(get_terminal_conversation_retirement)

    async def persist_parent(self) -> None:
        await set_workspace_profile(self.workspace)

    async def broadcast_host_message(self, jid: str, text: str) -> None:
        del jid, text

    def admin_chat_jid(self) -> str:
        return "admin"

    async def get_canary_report(self, *, history_limit: int) -> dict[str, object]:
        return {}

    def get_plugin_manager(self) -> object:
        return object()

    def get_workspace(self, folder: str) -> WorkspaceProfile | None:
        return self.workspace if folder == self.workspace.folder else None

    def dispatch_scheduled_task(self, task: ScheduledTask) -> None:
        self.dispatched.append(task)

    def channels(self) -> list[DiscordThreadChannel]:
        return [self.channel]

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return self.workspace_map

    async def register_workspace(self, profile: WorkspaceProfile) -> None:
        self.workspace_map[profile.jid] = profile
        await set_workspace_profile(profile)

    async def unregister_workspace(self, jid: str) -> None:
        self.workspace_map.pop(jid, None)
        await delete_workspace_profile(jid)

    async def bind_session(self, folder: str, session_id: SessionId) -> None:
        self.bound_sessions.append((folder, session_id))

    async def retire_conversation_for_terminal(
        self,
        conversation_id: ConversationId,
        *,
        preserve_delivery: ExternalDeliveryIdentity,
        control_state_revision: str | None,
    ) -> TerminalConversationRetirement:
        return await retire_terminal_state(
            conversation_id,
            preserve_delivery=preserve_delivery,
            control_state_revision=control_state_revision,
        )

    async def retire_conversation_runtime(self, folder: str) -> None:
        self.retired_folders.append(folder)

    async def retire_conversation_tasks(self, conversation_id: str) -> None:
        self.retired_task_conversations.append(conversation_id)

    async def ingest_message(self, jid: str, message: NewMessage) -> None:
        assert jid == message.chat_jid
        self.ingested.append(message)


class CursorDeps:
    def __init__(self) -> None:
        self.last_agent_timestamp: dict[str, str] = {}

    async def save_state(self) -> None: ...


def public_runtime() -> ControlPlaneRuntime:
    return ControlPlaneRuntime(
        bind_host="0.0.0.0",  # noqa: S104 - exercise public-bind auth policy
        port=8484,
        unix_socket=None,
        public_bind=True,
        remote_auth_required=True,
        allow_remote_deploy=False,
        auth_token=ControlPlaneToken("control-plane-token-that-is-long-enough"),
        rate_limiter=RequestRateLimiter(request_limit=100, window_seconds=60),
        audit_security_event=AsyncMock(),
    )


async def post_linear_event(
    client: TestClient,
    body: dict[str, Any],
    *,
    delivery_id: str = DELIVERY_ID,
) -> tuple[int, dict[str, Any]]:
    raw_body, headers = signed_request(body, delivery_id=delivery_id)
    response = await client.post("/webhooks/linear/project", data=raw_body, headers=headers)
    return response.status, await response.json()
