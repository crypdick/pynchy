"""Typed, host-only Pynchy actions for a deliberately narrow Gog surface."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pluggy
from pydantic import ValidationError

from pynchy.actions.api import ActionId
from pynchy.plugins.api import (
    ActionIntentContract,
    ActionIntentDraft,
    ActionIntentReceipt,
    ApprovalContract,
    AuditContract,
    CapabilityDescriptor,
    CapabilityId,
    CapabilityKind,
    CapabilityProbeContext,
    CapabilityProbeResult,
    CapabilityRequirement,
    CapabilityRequirementKind,
    HostActionAccess,
    HostActionDescriptor,
    HostActionRegistration,
    HostToolName,
    IdempotencyContract,
    IdempotencyMode,
    ProbeStatus,
)
from pynchy.plugins.integrations.gog._client import gog_executable_exists
from pynchy.plugins.integrations.gog._config import gog_runtime
from pynchy.plugins.integrations.gog._handlers import (
    handle_contacts_search,
    handle_docs_export,
    handle_docs_read,
    handle_gmail_create_draft,
    handle_gmail_get,
    handle_gmail_search,
    handle_gmail_send,
    handle_gmail_send_draft,
    handle_setup_complete,
    handle_setup_start,
    handle_sheets_get,
    handle_sheets_update,
)
from pynchy.plugins.integrations.gog._models import (
    DraftArguments,
    MailArguments,
    SheetUpdateArguments,
    request_arguments,
)

hookimpl = pluggy.HookimplMarker("pynchy")
type GogHandler = Callable[[dict[str, Any]], Awaitable[dict[str, object]]]
type IntentFactory = Callable[[dict[str, Any]], ActionIntentDraft]
type ActionDefinition = tuple[str, str, str, HostActionAccess, GogHandler, IntentFactory | None]


def _workspace_enables_gog(data: dict[str, Any]) -> bool:
    source_group = data.get("source_group")
    if not isinstance(source_group, str) or not source_group:
        return False
    return gog_runtime().workspace_enables_gog(source_group)


def _only_in_enabled_workspace(tool_name: str, handler: GogHandler) -> GogHandler:
    async def guarded(data: dict[str, Any]) -> dict[str, object]:
        if not _workspace_enables_gog(data):
            return {"error": f"{tool_name} is not enabled for this workspace"}
        return await handler(data)

    return guarded


async def _probe_gog(_context: CapabilityProbeContext) -> CapabilityProbeResult:
    """Check local configuration and executable presence without contacting Google."""
    try:
        config = gog_runtime().config
    except (ValidationError, ValueError):
        return CapabilityProbeResult(
            ProbeStatus.UNAVAILABLE,
            "Gog plugin options are invalid; follow the Google Workspace guide.",
        )
    if config.account is None:
        return CapabilityProbeResult(
            ProbeStatus.UNAVAILABLE,
            "Configure plugins.gog.options.account for the intended Google account.",
        )
    available = await asyncio.to_thread(gog_executable_exists, config.command)
    if not available:
        return CapabilityProbeResult(
            ProbeStatus.UNAVAILABLE,
            "Gog is unavailable; install gogcli or configure plugins.gog.options.command.",
        )
    return CapabilityProbeResult(ProbeStatus.READY)


def _intent_from_mail(data: dict[str, Any], *, operation: str) -> ActionIntentDraft:
    arguments = request_arguments(MailArguments, data)
    return ActionIntentDraft(
        recipient=",".join(arguments.to),
        payload=arguments.model_dump(mode="json"),
        summary=f"{operation} Gmail message to {', '.join(arguments.to)}",
    )


def _intent_from_draft(data: dict[str, Any]) -> ActionIntentDraft:
    arguments = request_arguments(DraftArguments, data)
    return ActionIntentDraft(
        recipient=f"draft:{arguments.draft_id}",
        payload=arguments.model_dump(mode="json"),
        summary=f"Send Gmail draft {arguments.draft_id}",
    )


def _intent_from_sheet_update(data: dict[str, Any]) -> ActionIntentDraft:
    arguments = request_arguments(SheetUpdateArguments, data)
    return ActionIntentDraft(
        recipient=f"sheet:{arguments.spreadsheet_id}/{arguments.range}",
        payload=arguments.model_dump(mode="json"),
        summary=f"Update Google Sheet range {arguments.range}",
    )


def _gog_receipt(response: dict[str, Any]) -> ActionIntentReceipt:
    raw_result = response.get("result")
    if not isinstance(raw_result, str):
        raise TypeError("Gog response omitted its serialized provider result")
    parsed = json.loads(raw_result)
    if not isinstance(parsed, dict | list):
        raise TypeError("Gog provider result must be a JSON object or array")
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    return ActionIntentReceipt(
        provider_request_id=_provider_request_id(parsed, canonical),
        receipt={"gog_result": parsed},
    )


def _provider_request_id(result: dict[str, Any] | list[Any], canonical: str) -> str:
    if isinstance(result, dict):
        for key in ("id", "messageId", "draftId", "updatedRange"):
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
        nested = result.get("result")
        if isinstance(nested, dict):
            return _provider_request_id(nested, canonical)
    return f"gog:{hashlib.sha256(canonical.encode()).hexdigest()}"


_ACTION_DEFINITIONS: tuple[ActionDefinition, ...] = (
    (
        "gog_setup_start",
        "integration.gog.auth.start",
        "Store the configured OAuth client and start Gog authorization.",
        HostActionAccess.WRITE,
        handle_setup_start,
        None,
    ),
    (
        "gog_setup_complete",
        "integration.gog.auth.complete",
        "Complete Gog authorization using a Google-returned redirect URL.",
        HostActionAccess.WRITE,
        handle_setup_complete,
        None,
    ),
    (
        "gog_gmail_search",
        "mail.gog.message.search",
        "Search Gmail threads through the configured Gog account.",
        HostActionAccess.READ,
        handle_gmail_search,
        None,
    ),
    (
        "gog_gmail_get",
        "mail.gog.message.read",
        "Read one sanitized Gmail message through Gog.",
        HostActionAccess.READ,
        handle_gmail_get,
        None,
    ),
    (
        "gog_gmail_create_draft",
        "mail.gog.draft.create",
        "Create a Gmail draft through the configured Gog account.",
        HostActionAccess.WRITE,
        handle_gmail_create_draft,
        lambda data: _intent_from_mail(data, operation="Create"),
    ),
    (
        "gog_gmail_send_draft",
        "mail.gog.draft.send",
        "Send a Gmail draft through the configured Gog account.",
        HostActionAccess.WRITE,
        handle_gmail_send_draft,
        _intent_from_draft,
    ),
    (
        "gog_gmail_send",
        "mail.gog.message.send",
        "Send a Gmail message through the configured Gog account.",
        HostActionAccess.WRITE,
        handle_gmail_send,
        lambda data: _intent_from_mail(data, operation="Send"),
    ),
    (
        "gog_contacts_search",
        "contacts.gog.contact.search",
        "Look up Google Contacts through the configured Gog account.",
        HostActionAccess.READ,
        handle_contacts_search,
        None,
    ),
    (
        "gog_docs_read",
        "docs.gog.document.read",
        "Read a Google Doc as text through Gog.",
        HostActionAccess.READ,
        handle_docs_read,
        None,
    ),
    (
        "gog_docs_export",
        "docs.gog.document.export",
        "Export a Google Doc as text, Markdown, or HTML through Gog.",
        HostActionAccess.READ,
        handle_docs_export,
        None,
    ),
    (
        "gog_sheets_get",
        "sheets.gog.range.read",
        "Read a Google Sheets range through Gog.",
        HostActionAccess.READ,
        handle_sheets_get,
        None,
    ),
    (
        "gog_sheets_update",
        "sheets.gog.range.write",
        "Update a Google Sheets range through Gog.",
        HostActionAccess.WRITE,
        handle_sheets_update,
        _intent_from_sheet_update,
    ),
)


def _descriptor(definition: ActionDefinition) -> HostActionDescriptor:
    tool_name, action_id, summary, access, handler, draft_factory = definition
    return HostActionDescriptor(
        capability=CapabilityDescriptor(
            id=CapabilityId(action_id),
            kind=CapabilityKind.HOST_ACTION,
            owner="gog",
            summary=summary,
            action_ids=(ActionId(action_id),),
            requirements=(
                CapabilityRequirement(
                    kind=CapabilityRequirementKind.WORKSPACE_TOOL,
                    name="gog",
                    description="Enable the Gog integration for this workspace.",
                ),
                CapabilityRequirement(
                    kind=CapabilityRequirementKind.HOST_BINARY,
                    name="gog",
                    description="Install gogcli or configure plugins.gog.options.command.",
                ),
                CapabilityRequirement(
                    kind=CapabilityRequirementKind.CREDENTIAL_REFERENCE,
                    name="Gog OAuth account",
                    description=(
                        "Authorize the configured Gog account through the host-only setup flow."
                    ),
                ),
            ),
            setup_hint=(
                "Follow the Google Workspace via Gog guide and enable the gog workspace tool."
            ),
            recovery_hint="Check the Gog executable and refresh the configured host OAuth session.",
            documentation="docs/integrations/google/workspace-gog.md",
            probe=_probe_gog,
        ),
        tool_name=HostToolName(tool_name),
        handler=_only_in_enabled_workspace(tool_name, handler),
        access=access,
        approval=ApprovalContract(),
        idempotency=IdempotencyContract(
            IdempotencyMode.NOT_REQUIRED
            if access is HostActionAccess.READ
            else IdempotencyMode.IPC_REQUEST_ID
        ),
        audit=AuditContract(),
        policy_service="gog",
        action_intent=(
            ActionIntentContract(
                provider="gog",
                draft_from_request=draft_factory,
                receipt_from_response=_gog_receipt,
            )
            if draft_factory is not None
            else None
        ),
    )


GOG_HOST_ACTIONS = HostActionRegistration(actions=tuple(map(_descriptor, _ACTION_DEFINITIONS)))


class GogWorkspacePlugin:
    """Expose a small host-only Gog capability surface to configured workspaces."""

    @hookimpl
    def pynchy_service_handler(self) -> HostActionRegistration:
        return GOG_HOST_ACTIONS
