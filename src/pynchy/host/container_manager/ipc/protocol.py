"""IPC protocol definitions — signal format and validation.

Tier 1 signals carry no payload; the host derives behavior from which
group sent the signal and from its own state.

Tier 2 requests carry a payload with a request_id for response tracking. A
future Deputy layer will mediate them before dispatch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 - beartype resolves IPC protocol paths at runtime.
from typing import Any, cast

from pynchy.identifiers import (
    ChatJid,
    GroupFolder,
    validate_request_id,
)
from pynchy.workspace.api import ContainerConfig

IPC_SCHEMA_VERSION = 1

UNKNOWN_SIGNAL_TYPE_MESSAGE = "Unknown signal type: {signal!r}"
SIGNAL_PAYLOAD_KEYS_MESSAGE = (
    "Signal {signal!r} contains unexpected payload keys: {extra_keys}. "
    "Signals must be payload-free."
)
MISSING_ENVELOPE_FIELDS_MESSAGE = "Missing IPC request envelope fields: {fields}"
UNSUPPORTED_SCHEMA_VERSION_MESSAGE = "Unsupported IPC request schema_version: {value!r}"
UNKNOWN_REQUEST_KIND_MESSAGE = "Unknown IPC request kind: {kind!r}"
NON_EMPTY_STRING_MESSAGE = "{label} must be a non-empty string"
STRING_OR_NULL_MESSAGE = "{label} must be a string or null"
PAYLOAD_OBJECT_MESSAGE = "IPC request envelope payload must be an object"
INVALID_SIGNAL_TYPE_MESSAGE = "Not a valid signal type: {signal_type!r}"

# Tier 1: Signal-only IPC types (no payload crosses the boundary)
SIGNAL_TYPES = frozenset(
    {
        "refresh_groups",
        # Future: "context_reset", "message_ready", "progress_ready"
    }
)

# Tier 2: Data-carrying IPC types (Deputy mediation planned)
TIER2_TYPES = frozenset(
    {
        "schedule_host_job",
        "deploy",
        "register_group",
        "create_periodic_agent",
        "messaging_source_health",
        "task_status",
        "task_definition",
        # Persistent learned-skill decisions use a host-only user approval record.
        "skill_access:policy",
        # Lifecycle: still carries data, will be reviewed later
        "reset_context",
        "sync_worktree_to_main",
        "publish_managed_feature",
        "rebase_managed_feature",
        # Task management
        "pause_task",
        "resume_task",
        "cancel_task",
        "update_scheduled_task",
        # Service requests (policy-gated, Step 2)
        "service:list_calendar",
        "service:create_event",
        "service:delete_event",
        # Slack token extraction
        "service:refresh_slack_tokens",
        "service:setup_slack_session",
        # X (Twitter) integration
        "service:setup_x_session",
        "service:x_post",
        "service:x_like",
        "service:x_reply",
        "service:x_retweet",
        "service:x_quote",
    }
)

REQUEST_KIND_PREFIXES = ("service:", "security:", "ask_user:")
READ_ONLY_REQUEST_PREFIXES = (
    "service:get_",
    "service:list_",
    "service:read_",
    "service:recall_",
    "skill_access:",
)
READ_ONLY_REQUEST_TYPES = frozenset({"messaging_source_health", "task_status", "task_definition"})


def validate_signal(data: dict[str, Any]) -> str | None:
    """Check if data is a valid Tier 1 signal.

    Returns the signal type if valid, None if it's not a signal
    (i.e. it's a Tier 2 data-carrying request).

    Raises ValueError if the file claims to be a signal but is malformed.
    """
    signal = data.get("signal")
    if signal is None:
        return None

    if signal not in SIGNAL_TYPES:
        raise ValueError(UNKNOWN_SIGNAL_TYPE_MESSAGE.format(signal=signal))

    # Signals must not carry payload data beyond the signal field itself
    extra_keys = set(data.keys()) - {"signal", "timestamp"}
    if extra_keys:
        raise ValueError(SIGNAL_PAYLOAD_KEYS_MESSAGE.format(signal=signal, extra_keys=extra_keys))

    return cast("str", signal)


def parse_ipc_file(file_path: Path) -> dict[str, Any]:
    """Read and parse a JSON IPC file.

    Returns the parsed data dict.
    Raises json.JSONDecodeError or OSError on failure.
    """
    return cast("dict[str, Any]", json.loads(file_path.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class IpcRequestEnvelope:
    """Canonical transport envelope for container-to-host IPC requests."""

    schema_version: int
    kind: str
    request_id: str
    source_group: GroupFolder
    created_at: str
    reply_to: str | None
    deadline: str | None
    payload: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IpcRequestEnvelope:
        _require_envelope_fields(data)
        schema_version = _envelope_schema_version(data["schema_version"])
        kind = _request_kind(data["kind"])
        request_id = validate_request_id(data["request_id"])
        source_group = GroupFolder(
            _required_string_field(
                "IPC request envelope source_group",
                data["source_group"],
            )
        )
        created_at = _required_string_field(
            "IPC request envelope created_at",
            data["created_at"],
        )
        reply_to = _optional_string_field("IPC request envelope reply_to", data["reply_to"])
        deadline = _optional_string_field("IPC request envelope deadline", data["deadline"])
        payload = _payload_object(data["payload"])

        return cls(
            schema_version=schema_version,
            kind=kind,
            request_id=request_id,
            source_group=source_group,
            created_at=created_at,
            reply_to=reply_to,
            deadline=deadline,
            payload=payload,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the envelope for an IPC request file."""
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "request_id": self.request_id,
            "source_group": self.source_group,
            "created_at": self.created_at,
            "reply_to": self.reply_to,
            "deadline": self.deadline,
            "payload": self.payload,
        }

    def to_handler_data(self) -> dict[str, Any]:
        """Return the handler-facing request data.

        The file transport uses ``kind``. Handler registration still uses the
        established ``type`` key, so this is the single conversion point between
        transport vocabulary and handler vocabulary.
        """
        data = dict(self.payload)
        data["type"] = self.kind
        data["request_id"] = self.request_id
        data["source_group"] = self.source_group
        data["reply_to"] = self.reply_to
        data["deadline"] = self.deadline
        return data


def _is_known_request_kind(kind: str) -> bool:
    """Return True when a request kind is routed by the IPC registry."""
    return (
        kind in SIGNAL_TYPES
        or kind in TIER2_TYPES
        or any(kind.startswith(prefix) for prefix in REQUEST_KIND_PREFIXES)
    )


def _require_envelope_fields(data: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "kind",
        "request_id",
        "source_group",
        "created_at",
        "reply_to",
        "deadline",
        "payload",
    }
    missing = sorted(required - set(data))
    if missing:
        raise ValueError(MISSING_ENVELOPE_FIELDS_MESSAGE.format(fields=", ".join(missing)))


def _envelope_schema_version(value: object) -> int:
    if value != IPC_SCHEMA_VERSION:
        raise ValueError(UNSUPPORTED_SCHEMA_VERSION_MESSAGE.format(value=value))
    return IPC_SCHEMA_VERSION


def _request_kind(value: object) -> str:
    kind = _required_string_field("IPC request envelope kind", value)
    if not _is_known_request_kind(kind):
        raise ValueError(UNKNOWN_REQUEST_KIND_MESSAGE.format(kind=kind))
    return kind


def _required_string_field(label: str, value: object) -> str:
    if isinstance(value, str) and value:
        return value
    raise ValueError(NON_EMPTY_STRING_MESSAGE.format(label=label))


def _optional_string_field(label: str, value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise ValueError(STRING_OR_NULL_MESSAGE.format(label=label))


def _payload_object(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return cast("dict[str, Any]", value)
    raise ValueError(PAYLOAD_OBJECT_MESSAGE)


def make_ipc_request(  # noqa: PLR0913 - canonical envelope builder keeps transport fields explicit.
    *,
    kind: str,
    request_id: str,
    source_group: str,
    payload: dict[str, Any] | None = None,
    created_at: str | None = None,
    reply_to: str | None = "responses",
    deadline: str | None = None,
) -> dict[str, Any]:
    """Create a canonical IPC request envelope."""
    if created_at is None:
        created_at = datetime.now(UTC).isoformat()
    envelope = IpcRequestEnvelope.from_dict(
        {
            "schema_version": IPC_SCHEMA_VERSION,
            "kind": kind,
            "request_id": request_id,
            "source_group": source_group,
            "created_at": created_at,
            "reply_to": reply_to,
            "deadline": deadline,
            "payload": payload or {},
        }
    )
    return envelope.to_dict()


def parse_request_envelope(file_path: Path) -> IpcRequestEnvelope:
    """Read a request file and parse its canonical transport envelope."""
    return IpcRequestEnvelope.from_dict(parse_ipc_file(file_path))


def request_requires_idempotency_ledger(kind: str) -> bool:
    """Return True when replaying this request kind could mutate host state."""
    if kind in READ_ONLY_REQUEST_TYPES or any(
        kind.startswith(prefix) for prefix in READ_ONLY_REQUEST_PREFIXES
    ):
        return False
    return not kind.startswith("security:")


def make_signal(signal_type: str) -> dict[str, str]:
    """Create a Tier 1 signal payload (for container-side use).

    This is the canonical format for signal-only IPC files.
    """
    if signal_type not in SIGNAL_TYPES:
        raise ValueError(INVALID_SIGNAL_TYPE_MESSAGE.format(signal_type=signal_type))
    return {"signal": signal_type}


# --- Handler payload models (parse, don't validate) ---
#
# ``IpcRequestEnvelope`` is the typed transport object for every request kind.
# The payload models below are handler-owned conveniences for places where a
# handler benefits from a narrower shape than ``dict[str, Any]``.


@dataclass(frozen=True)
class RegisterGroupRequest:
    """A validated ``register_group`` request."""

    jid: ChatJid
    name: str
    folder: GroupFolder
    trigger: str
    container_config: ContainerConfig | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RegisterGroupRequest | None:
        jid = data.get("jid")
        name = data.get("name")
        folder = data.get("folder")
        trigger = data.get("trigger")
        if not (jid and name and folder and trigger):
            return None
        raw_config = data.get("containerConfig")
        return cls(
            jid=ChatJid(jid),
            name=name,
            folder=GroupFolder(folder),
            trigger=trigger,
            container_config=ContainerConfig.from_dict(raw_config) if raw_config else None,
        )


@dataclass(frozen=True)
class CreatePeriodicAgentRequest:
    """A validated ``create_periodic_agent`` request.

    Cron validity of ``schedule`` is checked by the handler (which owns the
    ``croniter`` dependency and the distinct log message), not here.
    """

    name: str
    profile: str
    schedule: str
    prompt: str
    claude_md: str
    chat: str | None
    memory_enabled: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CreatePeriodicAgentRequest | None:
        name = data.get("name")
        profile = data.get("profile")
        schedule = data.get("schedule")
        prompt = data.get("prompt")
        memory_enabled = data.get("memory", True)
        if (
            not name
            or not profile
            or not schedule
            or not prompt
            or not isinstance(memory_enabled, bool)
        ):
            return None
        return cls(
            name=name,
            profile=profile,
            schedule=schedule,
            prompt=prompt,
            claude_md=data.get("claude_md", f"You are the {name} periodic agent."),
            chat=data.get("chat"),
            memory_enabled=memory_enabled,
        )


@dataclass(frozen=True)
class InboundChatMessage:
    """A ``type: message`` IPC file relaying text from a container to a chat."""

    chat_jid: ChatJid
    text: str
    sender: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InboundChatMessage | None:
        if data.get("type") != "message":
            return None
        chat_jid = data.get("chatJid")
        text = data.get("text")
        if not chat_jid or not text:
            return None
        sender = data.get("sender")
        return cls(
            chat_jid=ChatJid(chat_jid),
            text=text,
            sender=sender if isinstance(sender, str) else None,
        )
