"""Business tests for provider-neutral routed conversation foundations."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from conftest import init_test_database

from pynchy.conversation.models import (
    ControlSurface,
    ConversationControlBinding,
    ConversationDeliveryAdmission,
    ConversationId,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.identifiers import (
    ChatJid,
    GroupFolder,
)
from pynchy.plugins.api import (
    InboundFetchResult,
    OutboundEvent,
)
from pynchy.state import (
    WebhookReceipt,
    admit_conversation_delivery,
    admit_webhook_receipt,
    set_conversation_control_binding,
    set_workspace_profile,
    store_chat_metadata,
)
from pynchy.workspace.api import WorkspaceProfile


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


def _subject(key: str) -> ConversationSubject:
    return ConversationSubject(
        namespace=ConversationSubjectNamespace("linear:tenant-acme:issue"),
        key=ConversationSubjectKey(key),
    )


def _delivery(delivery_id: str) -> ExternalDeliveryIdentity:
    return ExternalDeliveryIdentity(
        provider=ExternalProvider("linear"),
        route=ExternalRoute("project"),
        delivery_id=ExternalDeliveryId(delivery_id),
    )


def _webhook_receipt(
    identity: ExternalDeliveryIdentity,
    subject_key: str,
    *,
    workspace: str = "triage",
    received_at: str = "2026-07-19T12:00:01+00:00",
) -> WebhookReceipt:
    return WebhookReceipt(
        provider=identity.provider,
        route=identity.route,
        delivery_id=identity.delivery_id,
        workspace=workspace,
        event_type="Issue",
        event_action="update",
        subject_id=subject_key,
        payload_sha256=f"sha-{identity.delivery_id}",
        disposition="notified",
        ignored_reason=None,
        task_id=None,
        occurred_at="2026-07-19T12:00:00+00:00",
        received_at=received_at,
    )


async def _admit(
    delivery_id: str,
    subject_key: str,
    workspace: str = "triage",
    *,
    received_at: str = "2026-07-19T12:00:01+00:00",
) -> ConversationDeliveryAdmission:
    identity = _delivery(delivery_id)
    await admit_webhook_receipt(
        _webhook_receipt(
            identity,
            subject_key,
            workspace=workspace,
            received_at=received_at,
        ),
        None,
    )
    admission = await admit_conversation_delivery(
        identity,
        _subject(subject_key),
        GroupFolder(workspace),
    )
    if admission is None:
        raise AssertionError("Ordinary test delivery was unexpectedly suppressed")
    return admission


async def _bind_control_thread(
    conversation_id: ConversationId,
    thread_jid: ChatJid,
    *,
    parent_workspace: GroupFolder | None = None,
) -> None:
    resolved_parent = parent_workspace or GroupFolder("triage")
    await store_chat_metadata(thread_jid, "2026-07-19T12:00:00+00:00")
    await set_conversation_control_binding(
        ConversationControlBinding(
            conversation_id=conversation_id,
            surface=ControlSurface.DISCORD,
            parent_workspace=resolved_parent,
            parent_jid=ChatJid(f"discord:channel:{resolved_parent}"),
            thread_jid=thread_jid,
            title="[SYN-9] Reset delivery ordering",
            updated_at="2026-07-19T12:00:00+00:00",
        )
    )


class _DiscordThreadChannel:
    name = "connection.discord.main"
    formatter = object()

    def __init__(self) -> None:
        self.threads: dict[tuple[str, str], str] = {}
        self.created: list[tuple[str, str, str]] = []

    async def connect(self) -> None: ...

    async def send_event(self, jid: str, event: OutboundEvent) -> None: ...

    def is_connected(self) -> bool:
        return True

    def owns_jid(self, jid: str) -> bool:
        return jid.startswith("discord:channel:")

    async def disconnect(self) -> None: ...

    async def reconnect(self) -> None: ...

    def prepare_shutdown(self) -> None: ...

    async def fetch_inbound_since(self, channel_jid: str, since: str) -> InboundFetchResult:
        return InboundFetchResult(messages=[])

    async def find_thread(self, parent_jid: str, name: str) -> str | None:
        return self.threads.get((parent_jid, name))

    async def create_thread(
        self,
        parent_jid: str,
        name: str,
        *,
        participant_ids: tuple[str, ...] = (),
    ) -> str:
        assert participant_ids == ()
        assert 1 <= len(name) <= 100
        thread_jid = f"discord:channel:thread-{len(self.created) + 1}"
        self.threads[parent_jid, name] = thread_jid
        self.created.append((parent_jid, name, thread_jid))
        return thread_jid


async def _register_workspace(jid: str, folder: str) -> None:
    await set_workspace_profile(
        WorkspaceProfile(
            jid=jid,
            name=folder.title(),
            folder=folder,
            trigger="@Pynchy",
            added_at=datetime.now(UTC).isoformat(),
        )
    )
