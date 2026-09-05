"""Aggregate-only marketplace state and reader-health projection."""

from __future__ import annotations

import asyncio
import json
from collections.abc import (
    Callable,  # noqa: TC003 - beartype resolves marketplace runtime callbacks at runtime.
)
from dataclasses import dataclass
from pathlib import (  # noqa: TC003 - Pydantic resolves field annotations at runtime.
    Path,
)
from typing import Any, Literal

import pluggy
from pydantic import BaseModel, ConfigDict, Field, field_validator

from pynchy.actions.api import ActionId
from pynchy.plugins.api import (
    ApprovalContract,
    AuditContract,
    CapabilityDescriptor,
    CapabilityId,
    CapabilityKind,
    CapabilityRequirement,
    CapabilityRequirementKind,
    HostActionAccess,
    HostActionDescriptor,
    HostActionRegistration,
    HostToolName,
    IdempotencyContract,
    IdempotencyMode,
)
from pynchy.plugins.integrations._service import service_tool
from pynchy.plugins.integrations.proton_bridge import create_proton_mail_client
from pynchy.plugins.integrations.proton_bridge_config import ProtonMailError

hookimpl = pluggy.HookimplMarker("pynchy")

_PLUGIN_NAME = "marketplace-health"
_TOOL_NAME = "marketplace_health_snapshot"
_ACTION_ID = "marketplace.health.read"
_MAX_STATE_BYTES = 1_000_000
type ReaderHealthReason = Literal[
    "ready",
    "reader_not_configured",
    "reader_credentials_unavailable",
    "reader_connection_unavailable",
    "reader_unavailable",
]


class MarketplaceHealthOptions(BaseModel):
    """Host-only paths and integration names needed by the projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pending_actions_file: Path
    reader_tool: str = Field(default="proton-mail", min_length=1)

    @field_validator("pending_actions_file")
    @classmethod
    def require_absolute_state_file(cls, path: Path) -> Path:
        """Keep state resolution independent from the service working directory."""
        if not path.is_absolute():
            raise ValueError("pending_actions_file must be absolute")
        return path


class MarketplaceCounts(BaseModel):
    """Only the two decision-state aggregates agents need."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pending: int = Field(ge=0)
    awaiting_reply: int = Field(ge=0)  # noqa: V107


class ReaderHealth(BaseModel):
    """Content-free availability result for the configured mail reader."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ready", "unavailable"]
    reason: ReaderHealthReason


class MarketplaceHealthSnapshot(BaseModel):
    """Aggregate marketplace state that cannot expose a buyer or email record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    counts: MarketplaceCounts
    reader_health: ReaderHealth  # noqa: V107


@dataclass(frozen=True)
class MarketplaceHealthRuntime:
    """Resolved marketplace projection configuration selected at composition."""

    options: MarketplaceHealthOptions | None
    reader_environment: Callable[[str], dict[str, str] | None]


@dataclass
class _RuntimeState:
    runtime: MarketplaceHealthRuntime | None = None


_runtime = _RuntimeState()


def configure_marketplace_health_runtime(runtime: MarketplaceHealthRuntime) -> None:
    """Set marketplace projection configuration before host actions run."""
    _runtime.runtime = runtime


def _configured_runtime() -> MarketplaceHealthRuntime:
    if _runtime.runtime is None:
        raise RuntimeError("Marketplace health runtime has not been configured")
    return _runtime.runtime


def _options() -> MarketplaceHealthOptions:
    options = _configured_runtime().options
    if options is None:
        raise ValueError("Marketplace health projection is not configured")
    return options


def _load_counts(path: Path) -> MarketplaceCounts:
    """Read only status labels from the buyer-action ledger and aggregate them."""
    try:
        if path.stat().st_size > _MAX_STATE_BYTES:
            raise ValueError("Marketplace action state exceeds the safe read limit")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Marketplace action state is unavailable") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("pending"), dict):
        raise TypeError("Marketplace action state has an invalid structure")

    counts = {"pending": 0, "awaiting_reply": 0}
    for entry in payload["pending"].values():
        if not isinstance(entry, dict):
            raise TypeError("Marketplace action state has an invalid entry")
        status = entry.get("status")
        if not isinstance(status, str):
            raise TypeError("Marketplace action state has an invalid status")
        if status in counts:
            counts[status] += 1
    return MarketplaceCounts.model_validate(counts)


def _reader_health(reader_tool: str) -> ReaderHealth:
    """Probe authentication and IMAP without fetching any message or mailbox content."""
    environment = _configured_runtime().reader_environment(reader_tool)
    if environment is None:
        return ReaderHealth(status="unavailable", reason="reader_not_configured")
    try:
        # list_mailboxes authenticates and performs IMAP LIST only. Discarding its
        # result keeps provider identifiers outside the agent-visible projection.
        create_proton_mail_client(environment=environment).list_mailboxes()
    except ProtonMailError as exc:
        message = str(exc).casefold()
        reason: ReaderHealthReason
        if "configure" in message:
            reason = "reader_not_configured"
        elif "password" in message:
            reason = "reader_credentials_unavailable"
        elif "imap" in message:
            reason = "reader_connection_unavailable"
        else:
            reason = "reader_unavailable"
        return ReaderHealth(status="unavailable", reason=reason)
    return ReaderHealth(status="ready", reason="ready")


def build_marketplace_health_snapshot(
    options: MarketplaceHealthOptions,
) -> MarketplaceHealthSnapshot:
    """Build a projection without changing state files or mail read flags."""
    return MarketplaceHealthSnapshot(
        counts=_load_counts(options.pending_actions_file),
        reader_health=_reader_health(options.reader_tool),
    )


@service_tool
async def _handle_marketplace_health_snapshot(
    _data: dict[str, Any],
) -> dict[str, Any]:
    snapshot = await asyncio.to_thread(build_marketplace_health_snapshot, _options())
    return {"result": snapshot.model_dump(mode="json")}


MARKETPLACE_HEALTH_HOST_ACTIONS = HostActionRegistration(
    actions=(
        HostActionDescriptor(
            capability=CapabilityDescriptor(
                id=CapabilityId(_ACTION_ID),
                kind=CapabilityKind.HOST_ACTION,
                owner=_PLUGIN_NAME,
                summary="Read aggregate marketplace decision counts and reader health.",
                action_ids=(ActionId(_ACTION_ID),),
                requirements=(
                    CapabilityRequirement(
                        kind=CapabilityRequirementKind.WORKSPACE_TOOL,
                        name=_PLUGIN_NAME,
                        description="Enable the marketplace health projection for this workspace.",
                    ),
                    CapabilityRequirement(
                        kind=CapabilityRequirementKind.CONFIG,
                        name=f"plugins.{_PLUGIN_NAME}.options.pending_actions_file",
                        description="Configure the host-owned marketplace action-state file.",
                    ),
                ),
                documentation="docs/integrations/marketplace-health.md",
            ),
            tool_name=HostToolName(_TOOL_NAME),
            handler=_handle_marketplace_health_snapshot,
            access=HostActionAccess.READ,
            approval=ApprovalContract(),
            idempotency=IdempotencyContract(IdempotencyMode.NOT_REQUIRED),
            audit=AuditContract(),
            policy_service=_PLUGIN_NAME,
        ),
    )
)


class MarketplaceHealthPlugin:
    """Expose a read-only aggregate health projection through host IPC."""

    def configure(self, runtime: MarketplaceHealthRuntime) -> None:
        """Set resolved runtime values before registering host actions."""
        configure_marketplace_health_runtime(runtime)

    @hookimpl
    def pynchy_service_handler(self) -> HostActionRegistration:
        return MARKETPLACE_HEALTH_HOST_ACTIONS
